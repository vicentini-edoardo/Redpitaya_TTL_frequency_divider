/*
 * rp_pll.c — Software Phase-Locked Loop for Red Pitaya STEMlab 125-14
 *
 * Acquires a TTL input (~15 kHz) on IN1, locks OUT1 to it with a
 * configurable phase offset and duty cycle, and exposes a TCP server
 * for remote control from a PC GUI.
 *
 * Build:  gcc -O2 -Wall -I/opt/redpitaya/include -o rp_pll rp_pll.c -lrp -lm -lpthread
 * Run:    ./rp_pll [phase_deg] [duty_cycle] [tcp_port]
 *         ./rp_pll 90 0.3 5555
 */

#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <math.h>
#include <pthread.h>
#include <stdatomic.h>
#include <stdbool.h>
#include <unistd.h>
#include <time.h>
#include <errno.h>
#include <sys/socket.h>
#include <netinet/in.h>
#include <arpa/inet.h>

#include <rp.h>

/* ── PLL constants ────────────────────────────────────────────────────────── */
#define KP              0.3         /* proportional gain                       */
#define KI              0.01        /* integral gain                           */
#define WINDUP_CLAMP    45.0        /* integrator anti-windup clamp (degrees)  */
#define EMA_ALPHA       0.05        /* exponential moving average, freq filter */
#define THRESHOLD_V     0.1         /* rising-edge detection threshold (volts) */
#define LOOP_SLEEP_MS   5           /* sleep between acquisitions (ms)         */
#define STATUS_INTERVAL_MS 100      /* TCP status push interval (ms)           */

/* ── ADC / buffer settings ────────────────────────────────────────────────── */
#define DECIMATION      RP_DEC_8            /* 125M/8 = 15.625 MSPS            */
#define BUF_SIZE        (16384)             /* samples per buffer               */
#define SAMPLE_RATE_HZ  (125000000.0 / 8.0) /* effective sample rate           */
#define FREQ_MEAS_BUFS  5                   /* buffers averaged for startup meas*/

/* ── TCP defaults ─────────────────────────────────────────────────────────── */
#define DEFAULT_PORT    5555
#define TCP_BACKLOG     1

/* ── Shared state (atomic, written by main thread, read by TCP thread) ────── */
static _Atomic double   g_phase_target = 0.0;   /* degrees, set by TCP        */
static _Atomic double   g_duty_cycle   = 0.5;   /* 0.0–1.0, set by TCP        */
static _Atomic bool     g_stop         = false;  /* set by TCP STOP command    */

/* ── Status struct (guarded by mutex for consistent snapshot) ─────────────── */
typedef struct {
    double freq;
    double phase_target;
    double phase_applied;
    double phase_error;
    double duty;
    bool   locked;
    long   uptime_s;
} Status;

static Status          g_status;
static pthread_mutex_t g_status_mutex = PTHREAD_MUTEX_INITIALIZER;

/* ── TCP server port (set from argv) ──────────────────────────────────────── */
static int g_tcp_port = DEFAULT_PORT;

/* ═══════════════════════════════════════════════════════════════════════════ */
/*  Utility helpers                                                            */
/* ═══════════════════════════════════════════════════════════════════════════ */

/* Wrap angle to [-180, +180] */
static double wrap_phase(double deg)
{
    while (deg >  180.0) deg -= 360.0;
    while (deg < -180.0) deg += 360.0;
    return deg;
}

/* Clamp a double to [lo, hi] */
static double clamp(double v, double lo, double hi)
{
    if (v < lo) return lo;
    if (v > hi) return hi;
    return v;
}

/* Monotonic millisecond counter */
static long long ms_now(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (long long)ts.tv_sec * 1000LL + ts.tv_nsec / 1000000LL;
}

/* Sleep for ms milliseconds */
static void sleep_ms(int ms)
{
    struct timespec ts = { ms / 1000, (ms % 1000) * 1000000L };
    nanosleep(&ts, NULL);
}

/* ═══════════════════════════════════════════════════════════════════════════ */
/*  Frequency measurement from an ADC buffer                                  */
/* ═══════════════════════════════════════════════════════════════════════════ */

/* Count rising edges in buf[0..n-1] and return estimated frequency (Hz).
 * Returns 0.0 if fewer than 2 edges are found. */
static double measure_freq(const float *buf, int n)
{
    int    edge_count   = 0;
    int    first_edge   = -1;
    int    last_edge    = -1;
    bool   above        = false;

    for (int i = 0; i < n; i++) {
        if (!above && buf[i] > THRESHOLD_V) {
            above = true;
            if (first_edge < 0) first_edge = i;
            last_edge = i;
            edge_count++;
        } else if (buf[i] < THRESHOLD_V) {
            above = false;
        }
    }

    if (edge_count < 2) return 0.0;

    double samples_per_cycle = (double)(last_edge - first_edge) / (edge_count - 1);
    return SAMPLE_RATE_HZ / samples_per_cycle;
}

/* ═══════════════════════════════════════════════════════════════════════════ */
/*  Output generation: write one cycle of a square wave to OUT1               */
/* ═══════════════════════════════════════════════════════════════════════════ */

/*
 * Generate a square wave on OUT1 for one cycle with the given
 * period (in samples at SAMPLE_RATE_HZ), duty cycle [0,1], and
 * phase_offset in degrees (applied as a sample delay into the cycle).
 *
 * Uses the rp_GenWaveform / rp_GenFreq / rp_GenAmp / rp_GenPhase API.
 * We configure the generator once per loop iteration with updated parameters.
 */
static void output_set(double freq_hz, double phase_deg, double duty)
{
    /* Red Pitaya generator: channel OUT1 */
    rp_GenFreq(RP_CH_1, (float)freq_hz);
    rp_GenAmp(RP_CH_1, 1.0f);               /* ±1V peak                       */
    rp_GenOffset(RP_CH_1, 0.0f);
    rp_GenWaveform(RP_CH_1, RP_WAVEFORM_SQUARE);
    rp_GenDutyCycle(RP_CH_1, (float)clamp(duty, 0.01, 0.99));
    rp_GenPhase(RP_CH_1, (float)phase_deg);
    rp_GenOutEnable(RP_CH_1);
}

/* ═══════════════════════════════════════════════════════════════════════════ */
/*  TCP server thread                                                          */
/* ═══════════════════════════════════════════════════════════════════════════ */

/* Build a STATUS JSON line into buf (must be at least 256 bytes). */
static int build_status_json(char *buf, size_t sz)
{
    Status s;
    pthread_mutex_lock(&g_status_mutex);
    s = g_status;
    pthread_mutex_unlock(&g_status_mutex);

    return snprintf(buf, sz,
        "STATUS {\"freq\":%.2f,\"phase_target\":%.1f,\"phase_applied\":%.1f,"
        "\"phase_error\":%.2f,\"duty\":%.3f,\"locked\":%s,\"uptime_s\":%ld}\n",
        s.freq, s.phase_target, s.phase_applied, s.phase_error,
        s.duty, s.locked ? "true" : "false", s.uptime_s);
}

/* Handle one connected client until it disconnects or sends STOP. */
static void handle_client(int fd)
{
    char     rxbuf[256];
    char     txbuf[512];
    int      rx_pos  = 0;
    long long next_status = ms_now();

    /* Set recv timeout so we can push periodic status even without commands */
    struct timeval tv = { 0, STATUS_INTERVAL_MS * 1000 };
    setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));

    while (!atomic_load(&g_stop)) {
        /* Push status on schedule */
        long long now = ms_now();
        if (now >= next_status) {
            int len = build_status_json(txbuf, sizeof(txbuf));
            if (send(fd, txbuf, len, MSG_NOSIGNAL) < 0) return;
            next_status = now + STATUS_INTERVAL_MS;
        }

        /* Try to receive data (non-blocking due to SO_RCVTIMEO) */
        ssize_t n = recv(fd, rxbuf + rx_pos, sizeof(rxbuf) - rx_pos - 1, 0);
        if (n == 0) return;                         /* client closed */
        if (n < 0) {
            if (errno == EAGAIN || errno == EWOULDBLOCK) continue; /* timeout */
            return;                                 /* real error             */
        }
        rx_pos += (int)n;
        rxbuf[rx_pos] = '\0';

        /* Process complete lines */
        char *line = rxbuf;
        char *nl;
        while ((nl = strchr(line, '\n')) != NULL) {
            *nl = '\0';
            /* Trim trailing \r */
            size_t ll = strlen(line);
            if (ll > 0 && line[ll-1] == '\r') line[ll-1] = '\0';

            /* ── Command dispatch ── */
            if (strncmp(line, "SET_PHASE ", 10) == 0) {
                double deg = atof(line + 10);
                if (deg < -360.0 || deg > 360.0) {
                    send(fd, "ERR phase out of range\n", 23, MSG_NOSIGNAL);
                } else {
                    atomic_store(&g_phase_target, deg);
                    send(fd, "OK\n", 3, MSG_NOSIGNAL);
                }
            } else if (strncmp(line, "SET_DUTY ", 9) == 0) {
                double duty = atof(line + 9);
                if (duty < 0.0 || duty > 1.0) {
                    send(fd, "ERR duty out of range\n", 22, MSG_NOSIGNAL);
                } else {
                    atomic_store(&g_duty_cycle, duty);
                    send(fd, "OK\n", 3, MSG_NOSIGNAL);
                }
            } else if (strcmp(line, "GET_STATUS") == 0) {
                int len = build_status_json(txbuf, sizeof(txbuf));
                send(fd, txbuf, len, MSG_NOSIGNAL);
            } else if (strcmp(line, "STOP") == 0) {
                send(fd, "OK\n", 3, MSG_NOSIGNAL);
                atomic_store(&g_stop, true);
                return;
            } else if (strlen(line) > 0) {
                send(fd, "ERR unknown command\n", 20, MSG_NOSIGNAL);
            }

            line = nl + 1;
        }

        /* Shift remaining partial line to front of buffer */
        int remaining = (int)(rxbuf + rx_pos - line);
        if (remaining > 0) memmove(rxbuf, line, remaining);
        else if (remaining < 0) remaining = 0;
        rx_pos = remaining;
    }
}

static void *tcp_thread(void *arg)
{
    (void)arg;

    int srv = socket(AF_INET, SOCK_STREAM, 0);
    if (srv < 0) {
        fprintf(stderr, "tcp_thread: socket: %s\n", strerror(errno));
        return NULL;
    }

    int opt = 1;
    setsockopt(srv, SOL_SOCKET, SO_REUSEADDR, &opt, sizeof(opt));

    struct sockaddr_in addr = {
        .sin_family      = AF_INET,
        .sin_port        = htons(g_tcp_port),
        .sin_addr.s_addr = INADDR_ANY,
    };
    if (bind(srv, (struct sockaddr *)&addr, sizeof(addr)) < 0) {
        fprintf(stderr, "tcp_thread: bind port %d: %s\n", g_tcp_port, strerror(errno));
        close(srv);
        return NULL;
    }
    listen(srv, TCP_BACKLOG);

    /* Accept-loop: one client at a time */
    while (!atomic_load(&g_stop)) {
        /* Use select with timeout so we can check g_stop periodically */
        fd_set rfds;
        FD_ZERO(&rfds);
        FD_SET(srv, &rfds);
        struct timeval tv = { 0, 200000 };  /* 200 ms */
        int sel = select(srv + 1, &rfds, NULL, NULL, &tv);
        if (sel <= 0) continue;

        struct sockaddr_in cli_addr;
        socklen_t cli_len = sizeof(cli_addr);
        int cli = accept(srv, (struct sockaddr *)&cli_addr, &cli_len);
        if (cli < 0) continue;

        handle_client(cli);
        close(cli);
    }

    close(srv);
    return NULL;
}

/* ═══════════════════════════════════════════════════════════════════════════ */
/*  Main: startup frequency measurement + PLL control loop                    */
/* ═══════════════════════════════════════════════════════════════════════════ */

int main(int argc, char *argv[])
{
    /* ── Parse arguments ── */
    double init_phase = 0.0;
    double init_duty  = 0.5;
    int    tcp_port   = DEFAULT_PORT;

    if (argc >= 2) init_phase = atof(argv[1]);
    if (argc >= 3) init_duty  = atof(argv[2]);
    if (argc >= 4) tcp_port   = atoi(argv[3]);

    init_phase = clamp(init_phase, -360.0, 360.0);
    init_duty  = clamp(init_duty,  0.01,   0.99);

    atomic_store(&g_phase_target, init_phase);
    atomic_store(&g_duty_cycle,   init_duty);
    g_tcp_port = tcp_port;

    /* ── Init Red Pitaya library ── */
    if (rp_Init() != RP_OK) {
        fprintf(stderr, "rp_Init failed\n");
        return 1;
    }

    /* ── Configure acquisition ── */
    rp_AcqReset();
    rp_AcqSetDecimation(DECIMATION);
    rp_AcqSetTriggerLevel(RP_CH_1, THRESHOLD_V);
    rp_AcqSetTriggerDelay(0);

    /* ── Allocate ADC buffer ── */
    float *buf = malloc(BUF_SIZE * sizeof(float));
    if (!buf) {
        fprintf(stderr, "malloc failed\n");
        rp_Release();
        return 1;
    }

    /* ── Startup: measure input frequency over FREQ_MEAS_BUFS buffers ── */
    double freq_sum = 0.0;
    int    freq_valid = 0;

    for (int b = 0; b < FREQ_MEAS_BUFS; b++) {
        rp_AcqStart();
        rp_AcqSetTriggerSrc(RP_TRIG_SRC_NOW);
        rp_acq_trig_state_t state = RP_TRIG_STATE_WAITING;
        /* Wait for buffer to fill (no more than 500ms per buffer) */
        for (int w = 0; w < 100 && state != RP_TRIG_STATE_TRIGGERED; w++) {
            usleep(5000);
            rp_AcqGetTriggerState(&state);
        }
        uint32_t n = BUF_SIZE;
        rp_AcqGetOldestDataV(RP_CH_1, &n, buf);
        rp_AcqStop();

        double f = measure_freq(buf, (int)n);
        if (f > 0.0) { freq_sum += f; freq_valid++; }
    }

    if (freq_valid == 0) {
        fprintf(stderr, "Could not detect input frequency on IN1\n");
        free(buf);
        rp_Release();
        return 1;
    }
    double base_freq = freq_sum / freq_valid;
    double ema_freq  = base_freq;   /* EMA-filtered frequency estimate         */

    /* ── Configure output generator with initial parameters ── */
    rp_GenReset();
    output_set(base_freq, init_phase, init_duty);

    /* ── Record start time ── */
    long long t_start = ms_now();

    /* ── Start TCP thread ── */
    pthread_t tcp_tid;
    pthread_create(&tcp_tid, NULL, tcp_thread, NULL);

    /* ── PI controller state ── */
    double integrator   = 0.0;
    double phase_applied = init_phase;

    /* ── PLL control loop ── */
    while (!atomic_load(&g_stop)) {
        /* Acquire one buffer */
        rp_AcqStart();
        rp_AcqSetTriggerSrc(RP_TRIG_SRC_NOW);
        rp_acq_trig_state_t state = RP_TRIG_STATE_WAITING;
        for (int w = 0; w < 40 && state != RP_TRIG_STATE_TRIGGERED; w++) {
            usleep(1000);
            rp_AcqGetTriggerState(&state);
        }
        uint32_t n = BUF_SIZE;
        rp_AcqGetOldestDataV(RP_CH_1, &n, buf);
        rp_AcqStop();

        /* Measure frequency */
        double meas_freq = measure_freq(buf, (int)n);
        bool   locked    = (meas_freq > 0.0);

        if (locked) {
            /* EMA filter on frequency */
            ema_freq = EMA_ALPHA * meas_freq + (1.0 - EMA_ALPHA) * ema_freq;
        }

        /* Read targets from atomic variables */
        double target_phase = atomic_load(&g_phase_target);
        double duty         = atomic_load(&g_duty_cycle);

        /* PI controller: compute phase error */
        double phase_error = wrap_phase(target_phase - phase_applied);

        /* Proportional + integral */
        integrator  = clamp(integrator + KI * phase_error, -WINDUP_CLAMP, WINDUP_CLAMP);
        double correction = KP * phase_error + integrator;

        phase_applied = wrap_phase(phase_applied + correction);

        /* Update output */
        output_set(ema_freq, phase_applied, duty);

        /* Update shared status */
        pthread_mutex_lock(&g_status_mutex);
        g_status.freq          = ema_freq;
        g_status.phase_target  = target_phase;
        g_status.phase_applied = phase_applied;
        g_status.phase_error   = phase_error;
        g_status.duty          = duty;
        g_status.locked        = locked;
        g_status.uptime_s      = (ms_now() - t_start) / 1000;
        pthread_mutex_unlock(&g_status_mutex);

        sleep_ms(LOOP_SLEEP_MS);
    }

    /* ── Cleanup ── */
    rp_GenOutDisable(RP_CH_1);
    rp_AcqStop();
    rp_Release();
    free(buf);

    pthread_join(tcp_tid, NULL);
    return 0;
}
