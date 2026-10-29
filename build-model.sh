#!/bin/sh
set -eu

cd -- "$(dirname -- "$0")"

# Build the model variant
ollama create "${1:-ops-gpt-7b}" -f ./Modelfile
