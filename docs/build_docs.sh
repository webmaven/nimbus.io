#!/bin/bash

set -x
set -e

DOCS_DIR="$(cd $(dirname $0) ; readlink -f $(pwd))"
CODEBASE="$(dirname $DOCS_DIR)"

export PYTHONPATH="${CODEBASE}"
export NIMBUSIO_NODE_NAME_SEQ=""
export NIMBUSIO_EVENT_PUBLISHER_PULL_ADDRESS=""
export NIMBUSIO_NODE_NAME=""
export NIMBUSIO_WEB_SERVER_PIPELINE_ADDRESS=""
export NIMBUSIO_DATA_READER_ADDRESSES=""
export NIMBUSIO_DATA_WRITER_ADDRESSES=""
export NIMBUSIO_SPACE_ACCOUNTING_SERVER_ADDRESS=""
export NIMBUSIO_SPACE_ACCOUNTING_PIPELINE_ADDRESS=""
export NIMBUSIO_WEB_SERVER_HOST=""
export NIMBUSIO_WEB_SERVER_PORT="8088"
export NIMBUSIO_EVENT_PUBLISHER_PULL_ADDRESS="" 
export NIMBUSIO_CLUSTER_NAME=""
export NIMBUSIO_LOG_DIR=""
export NIMBUSIO_EVENT_PUBLISHER_PUB_ADDRESS=""
export NIMBUSIO_ANTI_ENTROPY_SERVER_ADDRESSES=""
export NIMBUSIO_HANDOFF_SERVER_ADDRESSES=""
export NIMBUSIO_REPOSITORY_PATH=""
export NIMBUSIO_DATA_WRITER_ADDRESS=""
export NIMBUSIO_DATA_READER_ADDRESS=""
export NIMBUSIO_DATA_READER_ANTI_ENTROPY_ADDRESS=""
export NIMBUSIO_DATA_WRITER_ANTI_ENTROPY_ADDRESS=""
export NIMBUSIO_EVENT_AGGREGATOR_PUB_ADDRESS=""

pushd "${CODEBASE}/docs"
make clean
make html
popd
