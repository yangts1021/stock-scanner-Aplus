#!/bin/bash
set -e
cd /Users/rick/Developer/Aplus

# 把 stdout 直接送終端，並只取摘要區段
run_variant() {
    local label="$1"
    local rr="$2"
    local hold="$3"
    local body="$4"
    sed -i.bak "s/^MIN_RR.*$/MIN_RR         = $rr/" backtest_v7.py
    sed -i.bak "s/^HOLD_DAYS.*$/HOLD_DAYS      = $hold/" backtest_v7.py
    sed -i.bak "s/^MIN_BODY_MULT.*$/MIN_BODY_MULT    = $body/" scan_vectorized.py
    echo
    echo "########## $label ##########"
    python3 backtest_v7.py 2>&1 | tail -22
}

# Baseline 比對
run_variant "Baseline v7 (RR=2.0 HOLD=20 body=1.0)" 2.0 20 1.0

# Test 1: RR
run_variant "1a RR=2.5" 2.5 20 1.0
run_variant "1b RR=3.0" 3.0 20 1.0

# Test 2: HOLD
run_variant "2a RR=2.0 HOLD=30" 2.0 30 1.0
run_variant "2b RR=2.0 HOLD=40" 2.0 40 1.0

# Test 3: body strict
run_variant "3 RR=2.0 HOLD=20 body=1.5x" 2.0 20 1.5

# 還原
sed -i.bak "s/^MIN_RR.*$/MIN_RR         = 1.0/" backtest_v7.py
sed -i.bak "s/^HOLD_DAYS.*$/HOLD_DAYS      = 20/" backtest_v7.py
sed -i.bak "s/^MIN_BODY_MULT.*$/MIN_BODY_MULT    = 1.0/" scan_vectorized.py
rm -f backtest_v7.py.bak scan_vectorized.py.bak
