/*
 * rp_pulse_ctl.c — Red Pitaya NCO pulse generator register control helper
 *
 * Accesses the custom FPGA core via /dev/mem + mmap and prints all register
 * values as a single JSON object on stdout. The GUI calls this binary over SSH.
 *
 * Compile on the board:
 *   gcc -O2 -o /root/rp_pulse_ctl rp_pulse_ctl.c
 *
 * Register map (base address passed as first argument, default 0x40600000):
 *   0x00  control           bit 0 = output enable, bit 1 = soft reset (self-clearing)
 *   0x04  reserved
 *   0x08  width             pulse width in 125 MHz clock cycles
 *   0x0C  reserved
 *   0x10  status            bit 0 = busy, bit 1 = period_valid, bit 2 = timeout,
 *                           bit 3 = period_stable, bit 4 = freerun_active
 *   0x14  period            last raw measured input period (cycles)
 *   0x18  period_avg        IIR-filtered measured input period (cycles)
 *   0x1C  phase_step_offset_lo  bits [31:0]  of signed 48-bit NCO offset
 *   0x20  phase_step_offset_hi  bits [47:32] of signed 48-bit NCO offset (in [15:0])
 *   0x24  phase_step_base_lo    bits [31:0]  of computed base step (read-only)
 *   0x28  phase_step_base_hi    bits [47:32] of computed base step (in [15:0])
 *   0x2C  phase_step_lo         bits [31:0]  of live phase_step (read-only)
 *   0x30  phase_step_hi         bits [47:32] of live phase_step (in [15:0])
 *
 * Frequency from period:  freq_hz = 125000000.0 / period_cycles
 * NCO offset formula:     delta_f = phase_step_offset * 125e6 / 2^48
 * NCO resolution:         ~0.44 mHz per LSB at 125 MHz
 */

#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <fcntl.h>
#include <sys/mman.h>
#include <unistd.h>

#define REG_CONTROL               0x00
#define REG_WIDTH                 0x08
#define REG_STATUS                0x10
#define REG_RAW_PERIOD            0x14
#define REG_FILT_PERIOD           0x18
#define REG_PHASE_STEP_OFFSET_LO  0x1C
#define REG_PHASE_STEP_OFFSET_HI  0x20
#define REG_PHASE_STEP_BASE_LO    0x24
#define REG_PHASE_STEP_BASE_HI    0x28
#define REG_PHASE_STEP_LO         0x2C
#define REG_PHASE_STEP_HI         0x30

static void usage(const char *prog) {
    fprintf(stderr,
        "Usage:\n"
        "  %s <base_addr> read\n"
        "  %s <base_addr> write <width> <phase_step_offset> <control>\n"
        "      phase_step_offset: signed 48-bit integer\n"
        "      delta_f (Hz) = phase_step_offset * 125000000 / 2^48\n"
        "  %s <base_addr> soft_reset\n",
        prog, prog, prog);
}

static uint32_t rd32(volatile uint8_t *base, off_t off) {
    return *(volatile uint32_t *)(base + off);
}

static void wr32(volatile uint8_t *base, off_t off, uint32_t val) {
    *(volatile uint32_t *)(base + off) = val;
}

/* Read a 48-bit value split across two 32-bit registers (lo then hi[15:0]). */
static int64_t rd48(volatile uint8_t *base, off_t lo_off, off_t hi_off) {
    uint64_t lo = rd32(base, lo_off);
    uint64_t hi = rd32(base, hi_off) & 0xFFFFu;
    uint64_t raw = (hi << 32) | lo;
    /* Sign-extend from bit 47 */
    if (raw & (UINT64_C(1) << 47))
        raw |= ~((UINT64_C(1) << 48) - 1);
    return (int64_t)raw;
}

/* Write a 48-bit signed value into two 32-bit registers (lo then hi[15:0]).
   Write hi first so the FPGA never sees an inconsistent intermediate value
   (the active step is latched from lo). */
static void wr48(volatile uint8_t *base, off_t lo_off, off_t hi_off, int64_t val) {
    uint64_t bits = (uint64_t)val & ((UINT64_C(1) << 48) - 1);
    wr32(base, hi_off, (uint32_t)((bits >> 32) & 0xFFFFu));
    wr32(base, lo_off, (uint32_t)(bits & 0xFFFFFFFFu));
}

/* Print all registers as a JSON object. The GUI parses this output. */
static void print_json(volatile uint8_t *base) {
    const uint32_t status         = rd32(base, REG_STATUS);
    const uint32_t period_stable  = (status >> 3) & 0x1u;
    const uint32_t freerun_active = (status >> 4) & 0x1u;
    const int64_t  step_offset    = rd48(base, REG_PHASE_STEP_OFFSET_LO, REG_PHASE_STEP_OFFSET_HI);
    const int64_t  step_base      = rd48(base, REG_PHASE_STEP_BASE_LO,   REG_PHASE_STEP_BASE_HI);
    const int64_t  step_live      = rd48(base, REG_PHASE_STEP_LO,        REG_PHASE_STEP_HI);

    printf("{\"control\":%u,\"width\":%u,"
           "\"status\":%u,\"period_stable\":%u,\"freerun_active\":%u,"
           "\"raw_period\":%u,\"period_avg\":%u,"
           "\"phase_step_offset\":%lld,\"phase_step_base\":%lld,\"phase_step\":%lld}\n",
           rd32(base, REG_CONTROL),
           rd32(base, REG_WIDTH),
           status,
           period_stable,
           freerun_active,
           rd32(base, REG_RAW_PERIOD),
           rd32(base, REG_FILT_PERIOD),
           (long long)step_offset,
           (long long)step_base,
           (long long)step_live);
}

int main(int argc, char **argv) {
    int fd;
    void *map;
    volatile uint8_t *base;
    off_t phys;
    long page_size;
    off_t page_base;
    off_t page_off;

    if (argc < 3) {
        usage(argv[0]);
        return 1;
    }

    phys = (off_t)strtoull(argv[1], NULL, 0);
    page_size = sysconf(_SC_PAGESIZE);
    page_base = phys & ~((off_t)page_size - 1);
    page_off  = phys - page_base;

    fd = open("/dev/mem", O_RDWR | O_SYNC);
    if (fd < 0) {
        perror("open");
        return 1;
    }

    map = mmap(NULL, (size_t)page_size, PROT_READ | PROT_WRITE, MAP_SHARED, fd, page_base);
    if (map == MAP_FAILED) {
        perror("mmap");
        close(fd);
        return 1;
    }

    base = (volatile uint8_t *)map + page_off;

    if (strcmp(argv[2], "read") == 0) {
        print_json(base);

    } else if (strcmp(argv[2], "write") == 0) {
        /* write <width> <phase_step_offset> <control> */
        if (argc != 6) {
            usage(argv[0]);
            munmap(map, (size_t)page_size);
            close(fd);
            return 1;
        }
        uint32_t width            = (uint32_t)strtoul(argv[3], NULL, 0);
        int64_t  phase_step_offset = (int64_t)strtoll(argv[4], NULL, 0);
        uint32_t control          = (uint32_t)strtoul(argv[5], NULL, 0) & 0x1u;

        wr32(base, REG_CONTROL, 0);
        wr32(base, REG_WIDTH, width);
        wr48(base, REG_PHASE_STEP_OFFSET_LO, REG_PHASE_STEP_OFFSET_HI, phase_step_offset);
        wr32(base, REG_CONTROL, control);

        print_json(base);

    } else if (strcmp(argv[2], "soft_reset") == 0) {
        /* Bit 1 is self-clearing in the FPGA (single-cycle strobe). Preserve bit 0. */
        const uint32_t ctrl = rd32(base, REG_CONTROL) & ~0x2u;
        wr32(base, REG_CONTROL, ctrl | 0x2u);
        print_json(base);

    } else {
        usage(argv[0]);
        munmap(map, (size_t)page_size);
        close(fd);
        return 1;
    }

    munmap(map, (size_t)page_size);
    close(fd);
    return 0;
}
