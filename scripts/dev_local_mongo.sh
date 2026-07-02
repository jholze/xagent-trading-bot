#!/usr/bin/env bash
# Local Mac dev: never point demo/tests at Railway Mongo (prevents accidental wipes).
# Usage: source scripts/dev_local_mongo.sh
unset MONGO_URL
export MONGODB_URI="${MONGODB_URI:-mongodb://127.0.0.1:27017}"
export MONGODB_DB="${MONGODB_DB:-xagent_test}"
export MONGODB_TEST_DB="${MONGODB_TEST_DB:-xagent_test}"
echo "Local Mongo: ${MONGODB_URI} db=${MONGODB_DB} (MONGO_URL cleared)"