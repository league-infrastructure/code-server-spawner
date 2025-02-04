#!/bin/bash

names=(
    "alan_turing"
    "donald_knuth"
    "john_von_neumann"
    "tim_berners_lee"
    "claude_shannon"
    "kenneth_thompson"
    "dennis_ritchie"
    "linus_torvalds"
    "edsger_dijkstra"
    "vint_cerf"
)

for name in "${names[@]}"; do
    echo "Starting host: $name"
    cspawnctl host start  --no-wait "$name" 
done