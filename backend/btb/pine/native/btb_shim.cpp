// The one piece of C++ in btb: a flat C interface over a compiled PineForge
// strategy, for Python's ctypes.
//
// PineForge's report struct is large and versioned. Mirroring it field by
// field in ctypes would make Python responsible for its layout; compiling this
// file against pineforge.h makes the compiler responsible instead, and Python
// only ever sees doubles, ints and strings.
//
// One handle is one dlopen'd strategy. `btb_run` creates a fresh strategy
// instance per call, applies inputs and overrides, runs it over the bars it is
// given, copies the equity curve into the caller's buffer and keeps the trades
// for `btb_trades`. Nothing here is shared between handles, and a handle is
// only ever used from one thread.
#include <dlfcn.h>

#include <cstdio>
#include <cstring>
#include <string>
#include <vector>

#include "pineforge/pineforge.h"

namespace {

typedef int (*abi_fn)(void);
typedef pf_strategy_t (*create_fn)(const char*);
typedef void (*free_fn)(pf_strategy_t);
typedef void (*run_fn)(pf_strategy_t, pf_bar_t*, int, pf_report_t*);
typedef void (*rfree_fn)(pf_report_t*);
typedef void (*kv_fn)(pf_strategy_t, const char*, const char*);
typedef void (*ts_fn)(pf_strategy_t, int64_t);
typedef const char* (*err_fn)(pf_strategy_t);
typedef void (*str_fn)(pf_strategy_t, const char*);
typedef void (*dbl_fn)(pf_strategy_t, double);
typedef int (*feed_fn)(pf_strategy_t, const char*, const char*, const pf_bar_t*, const int64_t*, int);
typedef int (*fact_fn)(pf_strategy_t, const char*, const char*, const char*);

// Another symbol's bars for request.security, installed on every run.
struct Feed {
    std::string key, timeframe, type, mintick;
    std::vector<pf_bar_t> bars;
    std::vector<int64_t> close_ms;
};

struct Handle {
    void* lib = nullptr;
    create_fn create = nullptr;
    free_fn sfree = nullptr;
    run_fn run = nullptr;
    rfree_fn rfree = nullptr;
    kv_fn set_input = nullptr;
    kv_fn set_override = nullptr;
    ts_fn set_start = nullptr;
    err_fn last_error = nullptr;
    str_fn set_type = nullptr;
    str_fn set_tz = nullptr;
    str_fn set_session = nullptr;
    dbl_fn set_mintick = nullptr;
    feed_fn set_feed = nullptr;
    fact_fn set_fact = nullptr;
    std::vector<Feed> feeds;
    std::vector<pf_trade_t> trades;
};

void put(char* err, int len, const std::string& msg) {
    if (err && len > 0) snprintf(err, (size_t)len, "%s", msg.c_str());
}

template <typename T>
bool sym(Handle* h, const char* name, T& out, char* err, int len) {
    out = (T)dlsym(h->lib, name);
    if (!out) put(err, len, std::string("strategy library lacks ") + name);
    return out != nullptr;
}

}  // namespace

extern "C" {

// Number of doubles per trade row written by btb_trades.
int btb_trade_width(void) { return 10; }

void* btb_open(const char* so_path, char* err, int errlen) {
    Handle* h = new Handle();
    h->lib = dlopen(so_path, RTLD_NOW | RTLD_LOCAL);
    if (!h->lib) {
        const char* e = dlerror();
        put(err, errlen, e ? e : "dlopen failed");
        delete h;
        return nullptr;
    }
    abi_fn abi = (abi_fn)dlsym(h->lib, "pf_abi_version");
    if (!abi || abi() != PF_ABI_VERSION) {
        put(err, errlen, "strategy was built against a different PineForge ABI");
        dlclose(h->lib);
        delete h;
        return nullptr;
    }
    if (!sym(h, "strategy_create", h->create, err, errlen) ||
        !sym(h, "strategy_free", h->sfree, err, errlen) ||
        !sym(h, "run_backtest", h->run, err, errlen) ||
        !sym(h, "report_free", h->rfree, err, errlen) ||
        !sym(h, "strategy_set_input", h->set_input, err, errlen) ||
        !sym(h, "strategy_set_override", h->set_override, err, errlen) ||
        !sym(h, "strategy_set_trade_start_time", h->set_start, err, errlen) ||
        !sym(h, "strategy_get_last_error", h->last_error, err, errlen) ||
        !sym(h, "strategy_set_syminfo_type", h->set_type, err, errlen) ||
        !sym(h, "strategy_set_syminfo_timezone", h->set_tz, err, errlen) ||
        !sym(h, "strategy_set_syminfo_session", h->set_session, err, errlen) ||
        !sym(h, "strategy_set_syminfo_mintick", h->set_mintick, err, errlen) ||
        !sym(h, "strategy_set_symbol_feed", h->set_feed, err, errlen) ||
        !sym(h, "strategy_set_symbol_facts", h->set_fact, err, errlen)) {
        dlclose(h->lib);
        delete h;
        return nullptr;
    }
    return h;
}

// Keep another symbol's bars on the handle; every later btb_run installs
// them, keyed by the exact string the script's request.security passes.
int btb_add_feed(void* hv, const char* key, const char* timeframe, const char* sym_type,
                 const char* mintick, const pf_bar_t* bars, const int64_t* close_ms, int n) {
    Handle* h = (Handle*)hv;
    Feed f;
    f.key = key; f.timeframe = timeframe;
    f.type = sym_type ? sym_type : ""; f.mintick = mintick ? mintick : "";
    f.bars.assign(bars, bars + n);
    f.close_ms.assign(close_ms, close_ms + n);
    h->feeds.push_back(std::move(f));
    return (int)h->feeds.size();
}

void btb_close(void* hv) {
    Handle* h = (Handle*)hv;
    if (!h) return;
    if (h->lib) dlclose(h->lib);
    delete h;
}

// Returns the number of trades (>= 0), or -1 with `err` set. `equity_out` must
// hold `n` doubles; bars past what the engine processed are left as NaN.
// The symbol's facts matter: order prices are rounded to `mintick`, and
// session and timezone drive Pine's time functions. Empty / <= 0 keeps
// PineForge's default.
int btb_run(void* hv, pf_bar_t* bars, int n,
            int n_in, const char** in_keys, const char** in_vals,
            int n_ov, const char** ov_keys, const char** ov_vals,
            long long trade_start_ms,
            const char* sym_type, const char* timezone, const char* session, double mintick,
            double* equity_out, double* final_out,
            char* err, int errlen) {
    Handle* h = (Handle*)hv;
    h->trades.clear();
    pf_strategy_t s = h->create(nullptr);
    if (!s) {
        put(err, errlen, "strategy_create failed");
        return -1;
    }
    for (int i = 0; i < n_in; i++) h->set_input(s, in_keys[i], in_vals[i]);
    for (int i = 0; i < n_ov; i++) h->set_override(s, ov_keys[i], ov_vals[i]);
    if (trade_start_ms > 0) h->set_start(s, (int64_t)trade_start_ms);
    if (sym_type && *sym_type) h->set_type(s, sym_type);
    if (timezone && *timezone) h->set_tz(s, timezone);
    if (session && *session) h->set_session(s, session);
    if (mintick > 0) h->set_mintick(s, mintick);
    for (const Feed& f : h->feeds) {
        if (h->set_feed(s, f.key.c_str(), f.timeframe.c_str(), f.bars.data(), f.close_ms.data(),
                        (int)f.bars.size()) != 0) {
            const char* fe = h->last_error(s);
            put(err, errlen, std::string("could not install the bars for ") + f.key + ": " + (fe ? fe : ""));
            h->sfree(s);
            return -1;
        }
        if (!f.type.empty()) h->set_fact(s, f.key.c_str(), "type", f.type.c_str());
        if (!f.mintick.empty()) h->set_fact(s, f.key.c_str(), "mintick", f.mintick.c_str());
    }

    pf_report_t r;
    memset(&r, 0, sizeof r);
    h->run(s, bars, n, &r);
    const char* e = h->last_error(s);
    if (e && *e) {
        put(err, errlen, e);
        h->rfree(&r);
        h->sfree(s);
        return -1;
    }
    for (int i = 0; i < n; i++) equity_out[i] = __builtin_nan("");
    int64_t m = r.equity_curve_len < n ? r.equity_curve_len : n;
    for (int64_t i = 0; i < m; i++) equity_out[i] = r.equity_curve[i].equity;
    *final_out = m > 0 ? r.equity_curve[m - 1].equity : 0.0;
    h->trades.assign(r.trades, r.trades + r.trades_len);
    int count = r.trades_len;
    h->rfree(&r);
    h->sfree(s);
    return count;
}

// Trades of the last run, `btb_trade_width()` doubles each:
//   is_long, entry_bar, exit_bar, entry_price, exit_price, qty, pnl,
//   commission, open_at_end, entry_time_ms
int btb_trades(void* hv, double* out, int max_trades) {
    Handle* h = (Handle*)hv;
    int k = 0;
    for (const pf_trade_t& t : h->trades) {
        if (k >= max_trades) break;
        double* row = out + (size_t)k * 10;
        row[0] = t.is_long ? 1.0 : 0.0;
        row[1] = (double)t.entry_bar_index;
        row[2] = (double)t.exit_bar_index;
        row[3] = t.entry_price;
        row[4] = t.exit_price;
        row[5] = t.qty;
        row[6] = t.pnl;
        row[7] = t.commission;
        row[8] = (double)t.open_at_end;
        row[9] = (double)t.entry_time;
        k++;
    }
    return k;
}

}  // extern "C"
