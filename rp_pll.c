/*
 * rp_pll.c — Software Phase-Locked Loop for Red Pitaya STEMlab 125-14
 *
 * Acquires a TTL input on IN1, tracks its frequency on OUT1 with a
 * configurable phase offset and duty cycle, and exposes a TCP server
 * for remote control from a PC GUI.
 *
 * Build:  g++ -O2 -Wall -std=c++20 -I/boot/include -o rp_pll rp_pll.c \
 *             -L/boot/lib -Wl,-rpath,/boot/lib -lrp -lm -lpthread
 * Run:    ./rp_pll [phase_deg] [duty_cycle] [tcp_port]
 *         ./rp_pll 0 0.1 5555
 *         ./rp_pll --test-freq [duration_s]   (frequency stability test)
 *
 * Requires firmware 2.07. Load FPGA before running:
 *         /opt/redpitaya/sbin/overlay.sh v0.94
 */

#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <cmath>
#include <rp_hw_calib.h>
#include <pthread.h>
#include <atomic>
#include <unistd.h>
#include <time.h>
#include <errno.h>
#include <sys/socket.h>
#include <netinet/in.h>
#include <arpa/inet.h>

#include <rp.h>

using std::atomic;
using std::atomic_load;
using std::atomic_store;

/* ── PLL constants ────────────────────────────────────────────────────────── */
#define KP              0.3         /* proportional gain                       */
#define KI              0.01        /* integral gain                           */
#define WINDUP_CLAMP    45.0        /* integrator anti-windup clamp (degrees)  */
#define MEDIAN_WIN      9           /* median filter window for freq (odd)     */
#define FREQ_EMA_FAST   0.05        /* fast EMA (used first FREQ_WARMUP bufs)  */
#define FREQ_EMA_SLOW   0.005       /* slow EMA for steady-state precision     */
#define FREQ_WARMUP     50          /* buffers before switching to slow EMA    */
#define THRESHOLD_V     0.1         /* rising-edge detection threshold (volts) */
#define LOOP_SLEEP_MS   5           /* sleep between acquisitions (ms)         */
#define STATUS_INTERVAL_MS 100      /* TCP status push interval (ms)           */

/* ── ADC / buffer settings ────────────────────────────────────────────────── */
#define DECIMATION      RP_DEC_1024         /* 125M/1024 = ~122 kSPS (~1074 cycles/buf at 8kHz) */
#define BUF_SIZE        (16384)             /* samples per buffer               */
#define SAMPLE_RATE_HZ  (125000000.0 / 1024.0) /* effective sample rate        */
#define FREQ_MEAS_BUFS  5                   /* buffers averaged for startup meas*/

/* ── TCP defaults ─────────────────────────────────────────────────────────── */
#define DEFAULT_PORT    5555
#define TCP_BACKLOG     1

/* ── Shared state (atomic, written by main thread, read by TCP thread) ────── */
static atomic<double>   g_phase_target(0.0);    /* degrees, set by TCP        */
static atomic<double>   g_duty_cycle(0.5);      /* 0.0–1.0, set by TCP        */
static atomic<bool>     g_stop(false);          /* set by TCP STOP command    */

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

/* ── Scope buffer (last ADC snapshot, served by GET_SCOPE) ───────────────── */
#define SCOPE_BUF_MAX   1024
static float           g_scope_buf[SCOPE_BUF_MAX];
static int             g_scope_n   = 0;
static pthread_mutex_t g_scope_mutex = PTHREAD_MUTEX_INITIALIZER;

/* ── TCP server port (set from argv) ──────────────────────────────────────── */
static int g_tcp_port = DEFAULT_PORT;

/* ═══════════════════════════════════════════════════════════════════════════ */
/*  Utility helpers                                                            */
/* ═══════════════════════════════════════════════════════════════════════════ */

static double wrap_phase(double deg)
{
    while (deg >  180.0) deg -= 360.0;
    while (deg < -180.0) deg += 360.0;
    return deg;
}

static double clamp(double v, double lo, double hi)
{
    if (v < lo) return lo;
    if (v > hi) return hi;
    return v;
}

/* Median filter for frequency — insertion-sort window, no malloc */
static double g_freq_win[MEDIAN_WIN];
static int    g_freq_win_idx  = 0;
static int    g_freq_win_full = 0;

static double median_freq(double new_val)
{
    g_freq_win[g_freq_win_idx] = new_val;
    g_freq_win_idx = (g_freq_win_idx + 1) % MEDIAN_WIN;
    if (!g_freq_win_full && g_freq_win_idx == 0) g_freq_win_full = 1;
    int n = g_freq_win_full ? MEDIAN_WIN : g_freq_win_idx;
    double tmp[MEDIAN_WIN];
    for (int i = 0; i < n; i++) tmp[i] = g_freq_win[i];
    for (int i = 1; i < n; i++) {
        double key = tmp[i]; int j = i - 1;
        while (j >= 0 && tmp[j] > key) { tmp[j+1] = tmp[j]; j--; }
        tmp[j+1] = key;
    }
    return tmp[n / 2];
}

static long long ms_now(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (long long)ts.tv_sec * 1000LL + ts.tv_nsec / 1000000LL;
}

static void sleep_ms(int ms)
{
    struct timespec ts = { ms / 1000, (ms % 1000) * 1000000L };
    nanosleep(&ts, NULL);
}

/* ═══════════════════════════════════════════════════════════════════════════ */
/*  Frequency measurement from an ADC buffer                                  */
/* ═══════════════════════════════════════════════════════════════════════════ */

/*
 * Returns sample index (sub-sample interpolated) of the first rising edge.
 * Returns -1.0 if no edge found.
 */
static double first_rising_edge(const float *buf, int n)
{
    float vmin = buf[0], vmax = buf[0];
    for (int i = 1; i < n; i++) {
        if (buf[i] < vmin) vmin = buf[i];
        if (buf[i] > vmax) vmax = buf[i];
    }
    if ((vmax - vmin) < 0.05f) return -1.0;
    float vmid = (vmax + vmin) * 0.5f;
    float hyst = (vmax - vmin) * 0.3f;
    float hi = vmid + hyst, lo = vmid - hyst;
    bool above = (buf[0] > vmid);
    for (int i = 1; i < n; i++) {
        if (!above && buf[i] > hi) {
            double frac = (buf[i] == buf[i-1]) ? 0.0 :
                          (vmid - buf[i-1]) / (double)(buf[i] - buf[i-1]);
            return (i - 1) + frac;
        } else if (above && buf[i] < lo) {
            above = false;
        }
    }
    return -1.0;
}

/*
 * Adaptive hysteresis (30% of signal amplitude) + sub-sample interpolation.
 * Spans first-to-last edge for maximum averaging.
 * Returns 0.0 if fewer than 2 edges found.
 * out_edge_count may be NULL.
 */
static double measure_freq(const float *buf, int n, int *out_edge_count)
{
    float vmin = buf[0], vmax = buf[0];
    for (int i = 1; i < n; i++) {
        if (buf[i] < vmin) vmin = buf[i];
        if (buf[i] > vmax) vmax = buf[i];
    }
    float vmid = (vmax + vmin) * 0.5f;
    float hyst = (vmax - vmin) * 0.3f;
    if (hyst < 0.05f) {
        if (out_edge_count) *out_edge_count = 0;
        return 0.0;
    }
    float hi = vmid + hyst;
    float lo = vmid - hyst;

    int    edge_count = 0;
    double first_edge = -1.0;
    double last_edge  = -1.0;
    bool   above      = (buf[0] > vmid);

    for (int i = 1; i < n; i++) {
        if (!above && buf[i] > hi) {
            double frac = (buf[i] == buf[i-1]) ? 0.0 :
                          (vmid - buf[i-1]) / (double)(buf[i] - buf[i-1]);
            double edge = (i - 1) + frac;
            if (first_edge < 0.0) first_edge = edge;
            last_edge = edge;
            edge_count++;
            above = true;
        } else if (above && buf[i] < lo) {
            above = false;
        }
    }

    if (out_edge_count) *out_edge_count = edge_count;
    if (edge_count < 2) return 0.0;

    double samples_per_cycle = (last_edge - first_edge) / (edge_count - 1);
    return SAMPLE_RATE_HZ / samples_per_cycle;
}

/* ═══════════════════════════════════════════════════════════════════════════ */
/*  Output generation                                                          */
/* ═══════════════════════════════════════════════════════════════════════════ */

/* Called once at startup — sets waveform, amp, offset, enables output */
static void output_init(double freq_hz, double phase_deg, double duty)
{
    rp_GenWaveform(RP_CH_1, RP_WAVEFORM_PWM);
    rp_GenFreq(RP_CH_1, (float)freq_hz);
    rp_GenAmp(RP_CH_1, 1.0f);
    rp_GenOffset(RP_CH_1, 0.0f);
    rp_GenPhase(RP_CH_1, (float)phase_deg);
    rp_GenOutEnable(RP_CH_1);
    rp_GenDutyCycle(RP_CH_1, (float)clamp(duty, 0.01, 0.99));
}

/* Called each loop iteration — updates freq, phase, duty only */
static void output_set(double freq_hz, double phase_deg, double duty)
{
    rp_GenFreq(RP_CH_1, (float)freq_hz);
    rp_GenPhase(RP_CH_1, (float)phase_deg);
    rp_GenDutyCycle(RP_CH_1, (float)clamp(duty, 0.01, 0.99));
}

/* ═══════════════════════════════════════════════════════════════════════════ */
/*  TCP server thread                                                          */
/* ═══════════════════════════════════════════════════════════════════════════ */

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

static void handle_client(int fd)
{
    char      rxbuf[256];
    char      txbuf[512];
    int       rx_pos     = 0;
    long long next_status = ms_now();

    struct timeval tv = { 0, STATUS_INTERVAL_MS * 1000 };
    setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));

    while (!atomic_load(&g_stop)) {
        long long now = ms_now();
        if (now >= next_status) {
            int len = build_status_json(txbuf, sizeof(txbuf));
            if (send(fd, txbuf, len, MSG_NOSIGNAL) < 0) return;
            next_status = now + STATUS_INTERVAL_MS;
        }

        ssize_t n = recv(fd, rxbuf + rx_pos, sizeof(rxbuf) - rx_pos - 1, 0);
        if (n == 0) return;
        if (n < 0) {
            if (errno == EAGAIN || errno == EWOULDBLOCK) continue;
            return;
        }
        rx_pos += (int)n;
        rxbuf[rx_pos] = '\0';

        char *line = rxbuf;
        char *nl;
        while ((nl = strchr(line, '\n')) != NULL) {
            *nl = '\0';
            size_t ll = strlen(line);
            if (ll > 0 && line[ll-1] == '\r') line[ll-1] = '\0';

            if (strncmp(line, "SET_PHASE ", 10) == 0) {
                double deg = atof(line + 10);
                if (deg < -360.0 || deg > 360.0)
                    send(fd, "ERR phase out of range\n", 23, MSG_NOSIGNAL);
                else {
                    atomic_store(&g_phase_target, deg);
                    send(fd, "OK\n", 3, MSG_NOSIGNAL);
                }
            } else if (strncmp(line, "SET_DUTY ", 9) == 0) {
                double duty = atof(line + 9);
                if (duty < 0.0 || duty > 1.0)
                    send(fd, "ERR duty out of range\n", 22, MSG_NOSIGNAL);
                else {
                    atomic_store(&g_duty_cycle, duty);
                    send(fd, "OK\n", 3, MSG_NOSIGNAL);
                }
            } else if (strcmp(line, "GET_SCOPE") == 0) {
                pthread_mutex_lock(&g_scope_mutex);
                int   sn = g_scope_n;
                float lbuf[SCOPE_BUF_MAX];
                if (sn > 0) memcpy(lbuf, g_scope_buf, sn * sizeof(float));
                pthread_mutex_unlock(&g_scope_mutex);

                Status ss;
                pthread_mutex_lock(&g_status_mutex);
                ss = g_status;
                pthread_mutex_unlock(&g_status_mutex);

                int   rbsz = 256 + sn * 12;
                char *rb   = (char *)malloc(rbsz);
                if (!rb) {
                    send(fd, "ERR out of memory\n", 18, MSG_NOSIGNAL);
                } else {
                    int pos = snprintf(rb, rbsz,
                        "SCOPE {\"dt_us\":%.4f,\"freq\":%.2f,"
                        "\"phase\":%.1f,\"duty\":%.3f,\"v\":[",
                        1e6 / SAMPLE_RATE_HZ, ss.freq,
                        ss.phase_applied, ss.duty);
                    for (int i = 0; i < sn && pos < rbsz - 16; i++)
                        pos += snprintf(rb + pos, rbsz - pos,
                                        i < sn - 1 ? "%.3f," : "%.3f", lbuf[i]);
                    pos += snprintf(rb + pos, rbsz - pos, "]}\n");
                    send(fd, rb, pos, MSG_NOSIGNAL);
                    free(rb);
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

        int remaining = (int)(rxbuf + rx_pos - line);
        if (remaining > 0) memmove(rxbuf, line, remaining);
        else remaining = 0;
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

    struct sockaddr_in addr;
    memset(&addr, 0, sizeof(addr));
    addr.sin_family      = AF_INET;
    addr.sin_port        = htons(g_tcp_port);
    addr.sin_addr.s_addr = INADDR_ANY;
    if (bind(srv, (struct sockaddr *)&addr, sizeof(addr)) < 0) {
        fprintf(stderr, "tcp_thread: bind port %d: %s\n", g_tcp_port, strerror(errno));
        close(srv);
        return NULL;
    }
    listen(srv, TCP_BACKLOG);

    while (!atomic_load(&g_stop)) {
        fd_set rfds;
        FD_ZERO(&rfds);
        FD_SET(srv, &rfds);
        struct timeval tv = { 0, 200000 };
        if (select(srv + 1, &rfds, NULL, NULL, &tv) <= 0) continue;

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
/*  Frequency stability test mode (--test-freq)                               */
/* ═══════════════════════════════════════════════════════════════════════════ */

static void print_allan_deviation(const double *freqs, int n, double tau0_s)
{
    int taus[] = {1, 2, 4, 8, 16, 32, 0};
    fprintf(stderr, "FREQ_TEST ALLAN_DEV (tau, ADEV_Hz, ADEV_ppm):\n");
    for (int ti = 0; taus[ti] != 0; ti++) {
        int m = taus[ti];
        int N = n / m;
        if (N < 3) break;
        double sum_sq = 0.0;
        int    cnt    = 0;
        for (int i = 0; i < N - 1; i++) {
            double y1 = 0.0, y2 = 0.0;
            for (int j = 0; j < m; j++) {
                y1 += freqs[i * m + j];
                y2 += freqs[(i + 1) * m + j];
            }
            double d = y2 / m - y1 / m;
            sum_sq += d * d;
            cnt++;
        }
        if (cnt == 0) break;
        double adev = sqrt(sum_sq / (2.0 * cnt));
        double mean_ref = freqs[0];  /* approximate reference for ppm */
        fprintf(stderr, "  tau=%.3fs  ADEV=%.4fHz  (%.2fppm)\n",
                m * tau0_s, adev, mean_ref > 0.0 ? adev / mean_ref * 1e6 : 0.0);
    }
}

static int run_freq_test(int duration_s)
{
    fprintf(stderr,
        "FREQ_TEST starting: duration=%ds  decimation=RP_DEC_1024"
        "  buf_size=%d  sample_rate=%.0fHz\n",
        duration_s, BUF_SIZE, SAMPLE_RATE_HZ);

    system("/opt/redpitaya/bin/generate 1 1.0 1000 sqr");
    if (rp_InitReset(false) != RP_OK) {
        fprintf(stderr, "run_freq_test: rp_InitReset failed\n");
        return 1;
    }
    rp_CalibInit();
    rp_AcqReset();
    rp_AcqSetDecimation(DECIMATION);
    rp_AcqSetTriggerLevel(RP_T_CH_1, THRESHOLD_V);
    rp_AcqSetTriggerDelay(0);

    float  *buf          = (float  *)malloc(BUF_SIZE * sizeof(float));
    int     max_bufs     = duration_s * 250;
    double *freq_results = (double *)malloc(max_bufs * sizeof(double));
    int    *edge_results = (int    *)malloc(max_bufs * sizeof(int));

    if (!buf || !freq_results || !edge_results) {
        fprintf(stderr, "run_freq_test: malloc failed\n");
        free(buf); free(freq_results); free(edge_results);
        rp_Release();
        return 1;
    }

    long long t_start       = ms_now();
    long long t_end         = t_start + (long long)duration_s * 1000;
    long long t_next_report = t_start + 1000;

    int    buf_count = 0;
    double sec_sum = 0.0, sec_sum2 = 0.0;
    double sec_min = 1e18, sec_max = -1e18;
    int    sec_n = 0, sec_bad = 0;

    while (ms_now() < t_end) {
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

        int    ec = 0;
        double f  = measure_freq(buf, (int)n, &ec);

        if (buf_count < max_bufs) {
            freq_results[buf_count] = f;
            edge_results[buf_count] = ec;
        }
        buf_count++;

        if (ec < 10) sec_bad++;
        if (f > 0.0) {
            sec_sum  += f; sec_sum2 += f * f;
            if (f < sec_min) sec_min = f;
            if (f > sec_max) sec_max = f;
            sec_n++;
        }

        long long now = ms_now();
        if (now >= t_next_report) {
            double mean = (sec_n > 0) ? sec_sum / sec_n : 0.0;
            double var  = (sec_n > 1) ? (sec_sum2 - sec_n*mean*mean)/(sec_n-1) : 0.0;
            double std  = sqrt(var > 0.0 ? var : 0.0);
            fprintf(stderr,
                "FREQ_TEST 1s: n=%d  mean=%.4fHz  std=%.4fHz"
                "  min=%.4fHz  max=%.4fHz  bad=%d\n",
                sec_n, mean, std, sec_min, sec_max, sec_bad);
            sec_sum = sec_sum2 = 0.0;
            sec_min = 1e18; sec_max = -1e18;
            sec_n = sec_bad = 0;
            t_next_report = now + 1000;
        }

        sleep_ms(LOOP_SLEEP_MS);
    }

    int    use_n   = (buf_count < max_bufs) ? buf_count : max_bufs;
    int    valid_n = 0, bad_n = 0;
    double sum = 0.0, sum2 = 0.0, fmin = 1e18, fmax = -1e18;

    for (int i = 0; i < use_n; i++) {
        if (edge_results[i] < 10) bad_n++;
        double f = freq_results[i];
        if (f > 0.0) {
            sum += f; sum2 += f * f;
            if (f < fmin) fmin = f;
            if (f > fmax) fmax = f;
            valid_n++;
        }
    }

    double mean   = (valid_n > 0) ? sum / valid_n : 0.0;
    double var    = (valid_n > 1) ? (sum2 - valid_n*mean*mean)/(valid_n-1) : 0.0;
    double stddev = sqrt(var > 0.0 ? var : 0.0);

    fprintf(stderr,
        "FREQ_TEST SUMMARY: total_bufs=%d  valid=%d  bad_edge_count=%d\n"
        "  mean=%.4fHz  std=%.4fHz  min=%.4fHz  max=%.4fHz\n"
        "  range=%.4fHz  std_ppm=%.2f\n",
        buf_count, valid_n, bad_n,
        mean, stddev, fmin, fmax,
        fmax - fmin,
        (mean > 0.0) ? stddev / mean * 1e6 : 0.0);

    double tau0_s = BUF_SIZE / SAMPLE_RATE_HZ + LOOP_SLEEP_MS / 1000.0;
    print_allan_deviation(freq_results, use_n, tau0_s);

    free(freq_results);
    free(edge_results);
    free(buf);
    rp_Release();
    return 0;
}

/* ═══════════════════════════════════════════════════════════════════════════ */
/*  Main                                                                       */
/* ═══════════════════════════════════════════════════════════════════════════ */

int main(int argc, char *argv[])
{
    /* Check for --test-freq mode */
    for (int i = 1; i < argc; i++) {
        if (strcmp(argv[i], "--test-freq") == 0) {
            int dur = (i+1 < argc) ? atoi(argv[i+1]) : 10;
            return run_freq_test(dur > 0 ? dur : 10);
        }
    }

    /* Parse arguments */
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

    system("/opt/redpitaya/bin/generate 1 1.0 1000 sqr");
    if (rp_InitReset(false) != RP_OK) {
        fprintf(stderr, "rp_InitReset failed\n");
        return 1;
    }
    rp_CalibInit();

    rp_AcqReset();
    rp_AcqSetDecimation(DECIMATION);
    rp_AcqSetTriggerLevel(RP_T_CH_1, THRESHOLD_V);
    rp_AcqSetTriggerDelay(0);

    float *buf  = (float *)malloc(BUF_SIZE * sizeof(float));
    float *buf2 = (float *)malloc(BUF_SIZE * sizeof(float));
    if (!buf || !buf2) {
        fprintf(stderr, "malloc failed\n");
        free(buf); free(buf2);
        rp_Release();
        return 1;
    }

    long long t_start = ms_now();
    pthread_t tcp_tid;
    pthread_create(&tcp_tid, NULL, tcp_thread, NULL);

    /* Startup: measure input frequency, wait indefinitely for signal */
    double freq_sum   = 0.0;
    int    freq_valid = 0;

    while (freq_valid == 0 && !atomic_load(&g_stop)) {
        for (int b = 0; b < FREQ_MEAS_BUFS && !atomic_load(&g_stop); b++) {
            rp_AcqStart();
            rp_AcqSetTriggerSrc(RP_TRIG_SRC_NOW);
            rp_acq_trig_state_t state = RP_TRIG_STATE_WAITING;
            for (int w = 0; w < 100 && state != RP_TRIG_STATE_TRIGGERED; w++) {
                usleep(5000);
                rp_AcqGetTriggerState(&state);
            }
            uint32_t n = BUF_SIZE;
            rp_AcqGetOldestDataV(RP_CH_1, &n, buf);
            rp_AcqStop();

            double f = measure_freq(buf, (int)n, NULL);
            if (f > 0.0) { freq_sum += f; freq_valid++; }
        }
        if (freq_valid == 0) sleep_ms(500);
    }

    if (atomic_load(&g_stop)) {
        free(buf);
        rp_Release();
        pthread_join(tcp_tid, NULL);
        return 0;
    }

    double base_freq  = freq_sum / freq_valid;
    double filt_freq  = base_freq;

    /* Re-init DAC hardware at the measured frequency before taking over */
    {
        char cmd[128];
        snprintf(cmd, sizeof(cmd),
                 "/opt/redpitaya/bin/generate 1 1.0 %.1f sqr", base_freq);
        system(cmd);
        usleep(100000);
        rp_InitReset(false);
        rp_CalibInit();
    }
    output_init(base_freq, init_phase, init_duty);

    int loop_count = 0;

    /* Main loop: measure IN1 frequency with slow EMA, set OUT1, verify IN2 */
    while (!atomic_load(&g_stop)) {
        rp_AcqStart();
        rp_AcqSetTriggerSrc(RP_TRIG_SRC_NOW);
        rp_acq_trig_state_t state = RP_TRIG_STATE_WAITING;
        for (int w = 0; w < 40 && state != RP_TRIG_STATE_TRIGGERED; w++) {
            usleep(1000);
            rp_AcqGetTriggerState(&state);
        }
        uint32_t n = BUF_SIZE;
        rp_AcqGetOldestDataV(RP_CH_1, &n, buf);
        n = BUF_SIZE;
        rp_AcqGetOldestDataV(RP_CH_2, &n, buf2);
        rp_AcqStop();

        int snap = (int)n < SCOPE_BUF_MAX ? (int)n : SCOPE_BUF_MAX;
        pthread_mutex_lock(&g_scope_mutex);
        memcpy(g_scope_buf, buf, snap * sizeof(float));
        g_scope_n = snap;
        pthread_mutex_unlock(&g_scope_mutex);

        /* Measure IN1 frequency and update slow EMA */
        int    ec1       = 0;
        double meas_freq = measure_freq(buf, (int)n, &ec1);
        bool   locked    = (meas_freq > 0.0 && ec1 >= 5);
        if (locked) {
            double med   = median_freq(meas_freq);
            double alpha = (loop_count < FREQ_WARMUP) ? FREQ_EMA_FAST : FREQ_EMA_SLOW;
            filt_freq    = filt_freq + alpha * (med - filt_freq);
            loop_count++;
        }

        /* Measure IN2 (OUT1 feedback) frequency for closed-loop correction.
         * If OUT1 runs high/low, nudge the setpoint to compensate. */
        int    ec2    = 0;
        double freq2  = measure_freq(buf2, (int)n, &ec2);
        double freq_diff = 0.0;

        if (locked && ec2 >= 5 && freq2 > 0.0) {
            freq_diff = filt_freq - freq2;           /* positive = OUT1 too fast */
            /* Correct setpoint: shift by a fraction of the error each iteration */
            filt_freq += 0.1 * freq_diff;
        }

        double duty = atomic_load(&g_duty_cycle);

        /* Set OUT1 to the corrected frequency */
        output_set(filt_freq, init_phase, duty);

        pthread_mutex_lock(&g_status_mutex);
        g_status.freq          = filt_freq;
        g_status.phase_target  = init_phase;
        g_status.phase_applied = freq2;        /* reuse field to show IN2 freq */
        g_status.phase_error   = freq_diff;    /* reuse field to show freq diff */
        g_status.duty          = duty;
        g_status.locked        = locked;
        g_status.uptime_s      = (ms_now() - t_start) / 1000;
        pthread_mutex_unlock(&g_status_mutex);

        sleep_ms(LOOP_SLEEP_MS);
    }

    rp_GenOutDisable(RP_CH_1);
    rp_AcqStop();
    rp_Release();
    free(buf);
    free(buf2);
    pthread_join(tcp_tid, NULL);
    return 0;
}
