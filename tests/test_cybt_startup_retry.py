#!/usr/bin/env python3
"""Compile the actual cybt startup function against controlled hardware operations.

Requires a C compiler with UndefinedBehaviorSanitizer. These controls verify
startup ordering and error/resource handling; physical HCI behavior is separate.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import resource
import shlex
import subprocess
import tempfile


def extract_function(source, name):
    match = re.search(r"^[\w *]+\b" + name + r"\([^;]*?\)\s*\{", source, re.M)
    if match is None:
        raise ValueError(f"Function not found: {name}")
    depth, end = 1, match.end()
    while depth:
        depth += (source[end] == "{") - (source[end] == "}")
        end += 1
    return source[match.start():end]


STUB = r'''
#include <stdint.h>
#include <stdlib.h>
#include <assert.h>
typedef int cybt_result_t;
typedef int cyw43_ll_t;
#define CYBT_SUCCESS 0
#define CYBT_ERR_HCI_INIT_FAILED 99
#define CYW43_USE_HEX_BTFW 0
#define BTSDIO_FW_READY_POLLING_RETRY_COUNT 1
#define BTSDIO_FW_AWAKE_POLLING_RETRY_COUNT 1
#define cybt_error(...) ((void)0)
#define cybt_debug(...) ((void)0)
static int fail_stage, ready, prepared, downloaded, finished, host_ready;
static const uint8_t cyw43_btfw_43439[] = {0};
static const uint32_t cyw43_btfw_43439_len = 1;
static void cybt_sharedbus_driver_init(cyw43_ll_t *s) {(void)s;}
int cybt_ready(void) {return ready;}
static int cybt_fw_download_prepare(uint8_t **a, uint8_t **b) {
    (void)a; (void)b; prepared++; return fail_stage == 1 ? -1 : 0;
}
static int cybt_fw_download(const uint8_t *a, uint32_t b, uint8_t *c, uint8_t *d) {
    (void)a; (void)b; (void)c; (void)d; downloaded++;
    if (fail_stage == 2) {return -2;}
    ready = 1; return 0;
}
static void cybt_fw_download_finish(uint8_t *a, uint8_t *b) {(void)a; (void)b; finished++;}
static int cybt_wait_bt_ready(int n) {(void)n; return fail_stage == 3 ? -3 : 0;}
static int cybt_init_buffer(void) {return fail_stage == 4 ? -4 : 0;}
static int cybt_wait_bt_awake(int n) {(void)n; return fail_stage == 5 ? -5 : 0;}
static void cybt_set_host_ready(void) {host_ready++;}
static void cybt_toggle_bt_intr(void) {}
'''

MAIN = r'''
int main(int argc, char **argv) {
    assert(argc == 3);
    int mode = atoi(argv[1]);
    fail_stage = atoi(argv[2]);
    if (mode <= 1) {
        ready = mode;
        int effective = (ready && fail_stage < 3) ? 0 : fail_stage;
        int expected = effective == 0 ? 0 :
            (effective == 2 || effective == 3 ? 99 : -effective);
        assert(cyw43_btbus_init(NULL) == expected);
        assert(prepared == (mode ? 0 : 1));
        assert(downloaded == ((mode || fail_stage == 1) ? 0 : 1));
        assert(finished == downloaded);
        assert(host_ready == (expected == 0));
    } else {
        int expected = fail_stage == 0 ? 0 : (fail_stage == 3 ? 99 : -fail_stage);
        assert(cyw43_btbus_init(NULL) == expected);
        assert(downloaded == 1 && finished == 1);
        int before_host_ready = host_ready;
        if (mode == 3) {
            /* A controller reset clears the actual hardware-ready bit. */
            ready = 0;
        }
        fail_stage = 0;
        assert(cyw43_btbus_init(NULL) == 0);
        assert(downloaded == (mode == 3 ? 2 : 1));
        assert(prepared == downloaded && finished == downloaded);
        assert(host_ready == before_host_ready + 1);
    }
    return 0;
}
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[1] /
                        "src/rp2_common/pico_cyw43_driver/cybt_shared_bus/cybt_shared_bus.c")
    args = parser.parse_args()
    block = extract_function(args.source.read_text(), "cyw43_btbus_init")
    cases = [(initial, stage) for initial in (0, 1) for stage in range(6)]
    cases += [(2, stage) for stage in (3, 4, 5)] + [(3, stage) for stage in (0, 3)]
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    results = []
    with tempfile.TemporaryDirectory(prefix="cybt-startup-") as folder:
        source, binary = Path(folder) / "startup.c", Path(folder) / "startup"
        source.write_text(STUB + block + MAIN)
        subprocess.run(shlex.split(os.environ.get("CC", "cc")) +
                       ["-std=c11", "-Wall", "-Wextra", "-Werror", "-fsanitize=undefined",
                        str(source), "-o", str(binary)], check=True)
        for mode, stage in cases:
            run = subprocess.run([str(binary), str(mode), str(stage)], capture_output=True,
                                 text=True, timeout=10)
            results.append({"mode": mode, "failure_stage": stage,
                            "returncode": run.returncode, "stderr": run.stderr.strip()})
    print(json.dumps({"function_sha256": hashlib.sha256(block.encode()).hexdigest(),
                      "cases": results, "passed": sum(r["returncode"] == 0 for r in results)},
                     indent=2))
    return 0 if all(r["returncode"] == 0 for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
