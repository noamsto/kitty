#!/usr/bin/env bash
# dis.sh SO SYMBOL: disassembly of SYMBOL with addresses and call/jump targets masked
so=$1 sym=$2
objdump -d --no-show-raw-insn --disassemble="$sym" "$so" | sed -n '/^[0-9a-f]* <'"$sym"'>:/,/^$/p' | sed -E 's/^ *[0-9a-f]+:\t//; s/[0-9a-f]{4,} <([^>+]*)(\+0x[0-9a-f]+)?>/<\1>/g; s/0x[0-9a-f]{5,}/ADDR/g'
