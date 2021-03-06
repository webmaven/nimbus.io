# -*- coding: utf-8 -*-
"""
node_inspector_main.py
"""
import errno
import hashlib
import logging
import os
import sys

import zmq

from tools.standard_logging import initialize_logging
from tools.database_connection import get_node_local_connection
from tools.event_push_client import EventPushClient, unhandled_exception_topic
from tools.data_definitions import compute_expected_slice_count, \
        compute_value_file_path, \
        parse_timedelta_str, \
        create_timestamp, \
        damaged_segment_defective_sequence, \
        damaged_segment_missing_sequence

from anti_entropy.node_inspector.work_generator import \
        make_batch_key, generate_work

_local_node_name = os.environ["NIMBUSIO_NODE_NAME"]
_log_path = "{0}/nimbusio_node_inspector_{1}.log".format(
    os.environ["NIMBUSIO_LOG_DIR"], _local_node_name,
)
_repository_path = os.environ["NIMBUSIO_REPOSITORY_PATH"]
_max_value_file_time_str = \
        os.environ.get("NIMBUSIO_MAX_TIME_BETWEEN_VALUE_FILE_INTEGRITY_CHECK",
                       "weeks=1")
_max_value_file_time = None
_read_buffer_size = 1024 ** 2
_always_check_entries = bool(
    int(os.environ.get("NIMBUSIO_NODE_INSPECTOR_CHECK_ENTRIES", "0")))

# If the value file is missing, 
# consider all of the segment_sequences to be missing
_value_file_missing = 1

# If the value file exists, is closed, and has an md5 in the database, 
# has a size in the database, and the size in the stat matches the size in 
# the database, and has a close_time or a last_integrity_check_time that is 
# younger than (MAX_TIME_BETWEEN_VALUE_FILE_INTEGRITY_CHECK) 
# consider all records in the file undammaged
_value_file_valid = 2

# If none of the above branches were fruitful, then all records in the database 
# that point to this value file must be verified by opening, seeking, reading, 
# and hashing the record pointed to in the value file
_value_file_questionable = 3

def _store_damaged_segment(connection, entry, status, sequence_numbers):
    connection.execute("""
        insert into nimbusio_node.damaged_segment (
            collection_id,
            key,
            status,
            unified_id, 
            timestamp,
            segment_num,
            conjoined_part,
            sequence_numbers
        ) values (
            %(collection_id)s,
            %(key)s,
            %(status)s,
            %(unified_id)s, 
            %(timestamp)s,
            %(segment_num)s,
            %(conjoined_part)s,
            %(sequence_numbers)s
        )
    """, {"collection_id"   : entry.collection_id,
          "key"             : entry.key,
          "status"          : status,
          "unified_id"      : entry.unified_id, 
          "timestamp"       : entry.timestamp,
          "segment_num"     : entry.segment_num,
          "conjoined_part"  : entry.conjoined_part,
          "sequence_numbers": sequence_numbers,
    })

def _update_value_file_last_integrity_check_time(connection, 
                                                 value_file_id,
                                                 timestamp):
    connection.execute("""
        update nimbusio_node.value_file
        set last_integrity_check_time = %s
        where id = %s""", [timestamp, value_file_id, ])

def _value_file_status(connection, entry):
    log = logging.getLogger("_value_file_status")
    batch_key = make_batch_key(entry)

    value_file_path = compute_value_file_path(_repository_path, 
                                              entry.space_id, 
                                              entry.value_file_id)
    # Always do a stat on the value file. 
    try:
        stat_result = os.stat(value_file_path)
    except OSError as instance:
        # If the value file is missing, consider all of the segment_sequences 
        # to be missing, and handle it as such.
        if instance.errno == errno.ENOENT:
            log.error("value file missing {0} {1}".format(batch_key,
                                                          value_file_path))
            return _value_file_missing
        log.error("Error stat'ing value file {0} {1} {2}".format(
            str(instance), batch_key, value_file_path))
        raise

    # If the value file is still open, consider all data in it undammaged.
    if entry.value_file_close_time is None:
        return _value_file_valid

    # If the value file exists, is closed, and has an md5 in the database, 
    # has a size in the database, and the size in the stat matches the size 
    # in the database, and has a close_time or a last_integrity_check_time 
    # that is younger than (MAX_TIME_BETWEEN_VALUE_FILE_INTEGRITY_CHECK) 
    # consider all records in the file undammaged. (This is the common case.)
    if entry.value_file_hash is None:
        log.info("Value file row has no md5 hash {0} {1}".format(batch_key,
                                                                 entry))
        return _value_file_questionable

    if entry.value_file_size is None:
        log.info("Value file row has no size {0} {1}".format(batch_key,
                                                             entry))
        return _value_file_questionable

    if entry.value_file_size != stat_result.st_size:
        log.info("Value file row size {0} != stat size {1} {2}".format(
            entry.value_file_size, stat_result.st_size, batch_key))
        return _value_file_questionable
    
    current_time = create_timestamp()
    value_file_row_age = current_time - entry.value_file_close_time
    if entry.value_file_last_integrity_check_time is not None:
        value_file_row_age = \
                current_time - entry.value_file_last_integrity_check_time

    if value_file_row_age < _max_value_file_time:
        return _value_file_valid

    value_file_result = _value_file_valid

    # If the value matches all the previous criteria EXCEPT the 
    # MAX_TIME_BETWEEN_VALUE_FILE_INTEGRITY_CHECK, then read the whole file, 
    # and calculate the md5. If it matches, consider the whole file good as 
    # above. Update last_integrity_check_time regardless.

    md5_sum = hashlib.md5()
    try:
        with open(value_file_path, "rb") as input_file:
            while True:
                data = input_file.read(_read_buffer_size)
                if len(data) == 0:
                    break
                md5_sum.update(data)
    except (OSError, IOError) as instance:
        log.error("Error reading {0} {1}".format(value_file_path, 
                                                 instance))
        value_file_result =  _value_file_questionable

    if value_file_result == _value_file_valid and \
       md5_sum.digest() != bytes(entry.value_file_hash):
        log.error(
            "md5 mismatch {0} {1} {2} {3}".format(md5_sum.digest(),
                                                  bytes(entry.value_file_hash),
                                                  batch_key,
                                                  value_file_path))
        value_file_result =  _value_file_questionable


    # we're only supposed to do this after we've also read the file
    # and inserted any damage. not before. otherwise it's a race condition --
    # we may crash before finishing checking the file, and then the file
    # doesn't get checked, but it's marked as checked.

    _update_value_file_last_integrity_check_time(connection,
                                                 entry.value_file_id,
                                                 create_timestamp())

    return value_file_result

def _verify_entry_against_value_file(entry):
    log = logging.getLogger("_verify_entry_against_vaue_file")
    value_file_path = compute_value_file_path(_repository_path, 
                                              entry.space_id,
                                              entry.value_file_id)
    md5_sum = hashlib.md5()
    try:
        with open(value_file_path, "rb") as input_file:
            input_file.seek(entry.value_file_offset)
            md5_sum.update(input_file.read(entry.sequence_size))
    except (OSError, IOError) as instance:
        log.error("Error seek/reading {0} {1}".format(value_file_path, 
                                                      instance))
        return False
    return md5_sum.digest() == bytes(entry.sequence_hash)

def _process_work_batch(connection, known_value_files, batch):
    log = logging.getLogger("_process_work_batch")

    assert len(batch) > 0
    batch_key = make_batch_key(batch[0])
    log.info("batch {0}".format(batch_key))

    missing_sequence_numbers = list()
    defective_sequence_numbers = list()

    expected_slice_count = compute_expected_slice_count(batch[0].file_size)
    expected_sequence_numbers = set(range(0, expected_slice_count))
    actual_sequence_numbers = set([entry.sequence_num for entry in batch])
    missing_sequence_numbers.extend(
            list(expected_sequence_numbers - actual_sequence_numbers))

    for entry in batch:
        if not entry.value_file_id in known_value_files:
            known_value_files[entry.value_file_id] = \
                    _value_file_status(connection, entry)
        value_file_status = known_value_files[entry.value_file_id]

        # if we don't have a value_file for any sequence, 
        # treat that as missing too
        if value_file_status == _value_file_missing:
            log.info("Missing value file {0} for {1} sequence {2}".format(
                entry.value_file_id, batch_key, entry.sequence_num))
            missing_sequence_numbers.append(entry.sequence_num)
            continue

        if not _always_check_entries:
            if value_file_status == _value_file_valid:
                continue

            # if none of the above branches were fruitful, 
            # then all records in the database that point to this value file 
            # must be verified by opening, seeking, reading, and hashing the 
            # record pointed to in the value file. This will be terribly costly 
            # in terms of IO because our work is not sorted by value file. 
            # Fortunately, data corruption should be rare enough that the 
            # efficiency will be irrelevant
            assert value_file_status == _value_file_questionable

        if not _verify_entry_against_value_file(entry):
            log.info("Defective value file {0} for {1} sequence {2}".format(
                entry.value_file_id, batch_key, entry.sequence_num))
            defective_sequence_numbers.append(entry.sequence_num)
            continue

    if len(missing_sequence_numbers) > 0:
        missing_sequence_numbers.sort()
        log.info("missing sequence numbers {0}".format(
            missing_sequence_numbers))
        _store_damaged_segment(connection, 
                               batch[0], 
                               damaged_segment_missing_sequence,
                               missing_sequence_numbers)

    if len(defective_sequence_numbers) > 0:
        defective_sequence_numbers.sort()
        log.info("defective sequence numbers {0}".format(
            defective_sequence_numbers))
        _store_damaged_segment(connection, 
                               batch[0], 
                               damaged_segment_defective_sequence,
                               defective_sequence_numbers)

def main():
    """
    main entry point
    return 0 for success (exit code)
    """
    global _max_value_file_time

    initialize_logging(_log_path)
    log = logging.getLogger("main")

    try:
        _max_value_file_time = parse_timedelta_str(_max_value_file_time_str)
    except Exception as instance:
        log.exception("Unable to parse '{0}' {1}".format(
            _max_value_file_time_str, instance))
        return -1

    log.info("program starts; max_value_file_time = {0}".format(
        _max_value_file_time))

    zmq_context =  zmq.Context()

    event_push_client = EventPushClient(zmq_context, "node_inspector")
    event_push_client.info("program-start", "node_inspector starts")  

    try:
        connection = get_node_local_connection()
    except Exception as instance:
        log.exception("Exception connecting to database {0}".format(instance))
        event_push_client.exception(
            unhandled_exception_topic,
            str(instance),
            exctype=instance.__class__.__name__
        )
        return -1

    known_value_files = dict()

    connection.begin_transaction()
    try:
        for batch in generate_work(connection):
            _process_work_batch(connection, known_value_files, batch)
    except Exception as instance:
        connection.rollback()
        log.exception("Exception processing batch {0} {1}".format(
            batch, instance))
        event_push_client.exception(
            unhandled_exception_topic,
            str(instance),
            exctype=instance.__class__.__name__
        )
        return -1
    else:
        connection.commit()
    finally:
        connection.close()
        event_push_client.close()
        zmq_context.term()

    log.info("program terminates normally")
    return 0

if __name__ == "__main__":
    sys.exit(main())

