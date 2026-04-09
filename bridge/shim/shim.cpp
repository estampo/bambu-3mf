/**
 * shim.cpp — extern "C" wrappers for libbambu_networking.so
 *
 * The .so exports functions using C++ types (std::string, std::function,
 * std::map). This shim wraps each function with extern "C" using C-compatible
 * types so Rust can call them via FFI.
 *
 * The library is loaded at runtime via dlopen — no compile-time linking needed.
 */

#include <atomic>
#include <cstring>
#include <dlfcn.h>
#include <functional>
#include <map>
#include <string>

// ---------------------------------------------------------------------------
// Type definitions matching bambu_networking.hpp
// ---------------------------------------------------------------------------

typedef std::function<void(int status, int code, std::string msg)> OnUpdateStatusFn;
typedef std::function<bool()> WasCancelledFn;
typedef std::function<bool(int status, std::string job_info)> OnWaitFn;
typedef std::function<void(int online_login, bool login)> OnUserLoginFn;
typedef std::function<void(std::string topic_str)> OnPrinterConnectedFn;
typedef std::function<void(int return_code, int reason_code)> OnServerConnectedFn;
typedef std::function<void(unsigned http_code, std::string http_body)> OnHttpErrorFn;
typedef std::function<std::string()> GetCountryCodeFn;
typedef std::function<void(std::string topic)> GetSubscribeFailureFn;
typedef std::function<void(std::string dev_id, std::string msg)> OnMessageFn;

struct PrintParams {
    std::string dev_id;
    std::string task_name;
    std::string project_name;
    std::string preset_name;
    std::string filename;
    std::string config_filename;
    int         plate_index;
    std::string ftp_folder;
    std::string ftp_file;
    std::string ftp_file_md5;
    std::string nozzle_mapping;
    std::string ams_mapping;
    std::string ams_mapping2;
    std::string ams_mapping_info;
    std::string nozzles_info;
    std::string connection_type;
    std::string comments;
    int         origin_profile_id = 0;
    int         stl_design_id = 0;
    std::string origin_model_id;
    std::string print_type;
    std::string dst_file;
    std::string dev_name;
    std::string dev_ip;
    bool        use_ssl_for_ftp;
    bool        use_ssl_for_mqtt;
    std::string username;
    std::string password;
    bool        task_bed_leveling;
    bool        task_flow_cali;
    bool        task_vibration_cali;
    bool        task_layer_inspect;
    bool        task_record_timelapse;
    bool        task_use_ams;
    std::string task_bed_type;
    std::string extra_options;
    int         auto_bed_leveling{0};
    int         auto_flow_cali{0};
    int         auto_offset_cali{0};
    int         extruder_cali_manual_mode{-1};
    bool        task_ext_change_assist;
    bool        try_emmc_print;
};

// ---------------------------------------------------------------------------
// Function pointer types (resolved via dlsym)
// ---------------------------------------------------------------------------

typedef void* (*fn_create_agent)(std::string);
typedef int (*fn_destroy_agent)(void*);
typedef int (*fn_init_log)(void*);
typedef int (*fn_set_config_dir)(void*, std::string);
typedef int (*fn_set_cert_file)(void*, std::string, std::string);
typedef int (*fn_set_country_code)(void*, std::string);
typedef int (*fn_start)(void*);
typedef int (*fn_connect_server)(void*);
typedef bool (*fn_is_server_connected)(void*);
typedef int (*fn_change_user)(void*, std::string);
typedef bool (*fn_is_user_login)(void*);
typedef int (*fn_set_user_selected_machine)(void*, std::string);
typedef int (*fn_set_on_server_connected_fn)(void*, OnServerConnectedFn);
typedef int (*fn_set_on_http_error_fn)(void*, OnHttpErrorFn);
typedef int (*fn_set_on_message_fn)(void*, OnMessageFn);
typedef int (*fn_set_on_printer_connected_fn)(void*, OnPrinterConnectedFn);
typedef int (*fn_set_get_country_code_fn)(void*, GetCountryCodeFn);
typedef int (*fn_set_on_user_login_fn)(void*, OnUserLoginFn);
typedef int (*fn_set_on_subscribe_failure_fn)(void*, GetSubscribeFailureFn);
typedef int (*fn_set_extra_http_header)(void*, std::map<std::string, std::string>);
typedef int (*fn_send_message)(void*, std::string, std::string, int);
typedef int (*fn_send_message_to_printer)(void*, std::string, std::string, int, int);
typedef int (*fn_start_subscribe)(void*, std::string);
typedef int (*fn_start_print)(void*, PrintParams, OnUpdateStatusFn, WasCancelledFn, OnWaitFn);

// ---------------------------------------------------------------------------
// Resolved function pointers
// ---------------------------------------------------------------------------

static void* g_lib = nullptr;

static fn_create_agent              fp_create_agent = nullptr;
static fn_destroy_agent             fp_destroy_agent = nullptr;
static fn_init_log                  fp_init_log = nullptr;
static fn_set_config_dir            fp_set_config_dir = nullptr;
static fn_set_cert_file             fp_set_cert_file = nullptr;
static fn_set_country_code          fp_set_country_code = nullptr;
static fn_start                     fp_start = nullptr;
static fn_connect_server            fp_connect_server = nullptr;
static fn_is_server_connected       fp_is_connected = nullptr;
static fn_change_user               fp_change_user = nullptr;
static fn_is_user_login             fp_is_user_login = nullptr;
static fn_set_user_selected_machine fp_set_machine = nullptr;
static fn_set_on_server_connected_fn fp_set_server_cb = nullptr;
static fn_set_on_http_error_fn      fp_set_http_err_cb = nullptr;
static fn_set_on_message_fn         fp_set_message_cb = nullptr;
static fn_set_on_printer_connected_fn fp_set_printer_cb = nullptr;
static fn_set_get_country_code_fn   fp_set_country_cb = nullptr;
static fn_set_on_user_login_fn      fp_set_user_login_cb = nullptr;
static fn_set_on_subscribe_failure_fn fp_set_sub_fail_cb = nullptr;
static fn_set_extra_http_header     fp_set_extra_hdr = nullptr;
static fn_send_message              fp_send_msg = nullptr;
static fn_send_message_to_printer   fp_send_msg_printer = nullptr;
static fn_start_subscribe           fp_start_sub = nullptr;
static fn_start_print               fp_start_print = nullptr;

// ---------------------------------------------------------------------------
// Helper
// ---------------------------------------------------------------------------

template<typename T>
static T load_fn(const char* name) {
    void* ptr = dlsym(g_lib, name);
    return reinterpret_cast<T>(ptr);
}

// ---------------------------------------------------------------------------
// extern "C" API for Rust
// ---------------------------------------------------------------------------

extern "C" {

int bambu_shim_load(const char* lib_path) {
    if (g_lib) return 0; // already loaded

    g_lib = dlopen(lib_path, RTLD_LAZY);
    if (!g_lib) return -1;

    fp_create_agent    = load_fn<fn_create_agent>("bambu_network_create_agent");
    fp_destroy_agent   = load_fn<fn_destroy_agent>("bambu_network_destroy_agent");
    fp_init_log        = load_fn<fn_init_log>("bambu_network_init_log");
    fp_set_config_dir  = load_fn<fn_set_config_dir>("bambu_network_set_config_dir");
    fp_set_cert_file   = load_fn<fn_set_cert_file>("bambu_network_set_cert_file");
    fp_set_country_code = load_fn<fn_set_country_code>("bambu_network_set_country_code");
    fp_start           = load_fn<fn_start>("bambu_network_start");
    fp_connect_server  = load_fn<fn_connect_server>("bambu_network_connect_server");
    fp_is_connected    = load_fn<fn_is_server_connected>("bambu_network_is_server_connected");
    fp_change_user     = load_fn<fn_change_user>("bambu_network_change_user");
    fp_is_user_login   = load_fn<fn_is_user_login>("bambu_network_is_user_login");
    fp_set_machine     = load_fn<fn_set_user_selected_machine>("bambu_network_set_user_selected_machine");
    fp_set_server_cb   = load_fn<fn_set_on_server_connected_fn>("bambu_network_set_on_server_connected_fn");
    fp_set_http_err_cb = load_fn<fn_set_on_http_error_fn>("bambu_network_set_on_http_error_fn");
    fp_set_message_cb  = load_fn<fn_set_on_message_fn>("bambu_network_set_on_message_fn");
    fp_set_printer_cb  = load_fn<fn_set_on_printer_connected_fn>("bambu_network_set_on_printer_connected_fn");
    fp_set_country_cb  = load_fn<fn_set_get_country_code_fn>("bambu_network_set_get_country_code_fn");
    fp_set_user_login_cb = load_fn<fn_set_on_user_login_fn>("bambu_network_set_on_user_login_fn");
    fp_set_sub_fail_cb = load_fn<fn_set_on_subscribe_failure_fn>("bambu_network_set_on_subscribe_failure_fn");
    fp_set_extra_hdr   = load_fn<fn_set_extra_http_header>("bambu_network_set_extra_http_header");
    fp_send_msg        = load_fn<fn_send_message>("bambu_network_send_message");
    fp_send_msg_printer = load_fn<fn_send_message_to_printer>("bambu_network_send_message_to_printer");
    fp_start_sub       = load_fn<fn_start_subscribe>("bambu_network_start_subscribe");
    fp_start_print     = load_fn<fn_start_print>("bambu_network_start_print");

    if (!fp_create_agent || !fp_change_user || !fp_connect_server) {
        dlclose(g_lib);
        g_lib = nullptr;
        return -2;
    }
    return 0;
}

const char* bambu_shim_load_error() {
    return dlerror();
}

void* bambu_shim_create_agent(const char* log_dir) {
    if (!fp_create_agent) return nullptr;
    return fp_create_agent(std::string(log_dir));
}

int bambu_shim_destroy_agent(void* agent) {
    if (!fp_destroy_agent) return -1;
    return fp_destroy_agent(agent);
}

int bambu_shim_init_log(void* agent) {
    if (!fp_init_log) return -1;
    return fp_init_log(agent);
}

int bambu_shim_set_config_dir(void* agent, const char* dir) {
    if (!fp_set_config_dir) return -1;
    return fp_set_config_dir(agent, std::string(dir));
}

int bambu_shim_set_cert_file(void* agent, const char* dir, const char* name) {
    if (!fp_set_cert_file) return -1;
    return fp_set_cert_file(agent, std::string(dir), std::string(name));
}

int bambu_shim_set_country_code(void* agent, const char* code) {
    if (!fp_set_country_code) return -1;
    return fp_set_country_code(agent, std::string(code));
}

int bambu_shim_start(void* agent) {
    if (!fp_start) return -1;
    return fp_start(agent);
}

int bambu_shim_set_extra_http_header(
    void* agent, const char** keys, const char** vals, int count
) {
    if (!fp_set_extra_hdr) return -1;
    std::map<std::string, std::string> hdrs;
    for (int i = 0; i < count; i++) {
        hdrs[std::string(keys[i])] = std::string(vals[i]);
    }
    return fp_set_extra_hdr(agent, hdrs);
}

int bambu_shim_change_user(void* agent, const char* json) {
    if (!fp_change_user) return -1;
    return fp_change_user(agent, std::string(json));
}

int bambu_shim_is_user_login(void* agent) {
    if (!fp_is_user_login) return 0;
    return fp_is_user_login(agent) ? 1 : 0;
}

int bambu_shim_connect_server(void* agent) {
    if (!fp_connect_server) return -1;
    return fp_connect_server(agent);
}

int bambu_shim_is_server_connected(void* agent) {
    if (!fp_is_connected) return 0;
    return fp_is_connected(agent) ? 1 : 0;
}

int bambu_shim_set_user_selected_machine(void* agent, const char* dev_id) {
    if (!fp_set_machine) return -1;
    return fp_set_machine(agent, std::string(dev_id));
}

int bambu_shim_start_subscribe(void* agent, const char* module) {
    if (!fp_start_sub) return -1;
    return fp_start_sub(agent, std::string(module));
}

int bambu_shim_send_message(void* agent, const char* dev_id, const char* json, int qos) {
    if (!fp_send_msg) return -1;
    return fp_send_msg(agent, std::string(dev_id), std::string(json), qos);
}

int bambu_shim_send_message_to_printer(
    void* agent, const char* dev_id, const char* json, int qos, int timeout
) {
    if (!fp_send_msg_printer) return -1;
    return fp_send_msg_printer(
        agent, std::string(dev_id), std::string(json), qos, timeout
    );
}

// ---------------------------------------------------------------------------
// Callback setters
//
// Each stores a C function pointer + void* context, wraps it in a
// std::function, and passes it to the real .so function.
//
// IMPORTANT: Single-agent constraint
// The callback function pointers and context pointers (g_server_cb,
// g_message_cb, etc.) are file-scoped globals. This means only ONE agent
// instance can have callbacks registered at a time. If a second agent
// calls these setters, it silently overwrites the first agent's callbacks.
// This is acceptable because the Bambu networking .so itself only supports
// one agent per process.
// ---------------------------------------------------------------------------

// on_server_connected(int rc, int reason)
typedef void (*shim_on_server_connected_fn)(int, int, void*);
static shim_on_server_connected_fn g_server_cb = nullptr;
static void* g_server_cb_ctx = nullptr;

int bambu_shim_set_on_server_connected_fn(
    void* agent, shim_on_server_connected_fn cb, void* ctx
) {
    if (!fp_set_server_cb) return -1;
    g_server_cb = cb;
    g_server_cb_ctx = ctx;
    OnServerConnectedFn wrapper = [](int rc, int reason) {
        if (g_server_cb) g_server_cb(rc, reason, g_server_cb_ctx);
    };
    return fp_set_server_cb(agent, wrapper);
}

// on_message(dev_id, msg)
typedef void (*shim_on_message_fn)(const char*, const char*, void*);
static shim_on_message_fn g_message_cb = nullptr;
static void* g_message_cb_ctx = nullptr;

int bambu_shim_set_on_message_fn(
    void* agent, shim_on_message_fn cb, void* ctx
) {
    if (!fp_set_message_cb) return -1;
    g_message_cb = cb;
    g_message_cb_ctx = ctx;
    OnMessageFn wrapper = [](std::string dev_id, std::string msg) {
        if (g_message_cb) g_message_cb(dev_id.c_str(), msg.c_str(), g_message_cb_ctx);
    };
    return fp_set_message_cb(agent, wrapper);
}

// on_printer_connected(topic)
typedef void (*shim_on_printer_connected_fn)(const char*, void*);
static shim_on_printer_connected_fn g_printer_cb = nullptr;
static void* g_printer_cb_ctx = nullptr;

int bambu_shim_set_on_printer_connected_fn(
    void* agent, shim_on_printer_connected_fn cb, void* ctx
) {
    if (!fp_set_printer_cb) return -1;
    g_printer_cb = cb;
    g_printer_cb_ctx = ctx;
    OnPrinterConnectedFn wrapper = [](std::string topic) {
        if (g_printer_cb) g_printer_cb(topic.c_str(), g_printer_cb_ctx);
    };
    return fp_set_printer_cb(agent, wrapper);
}

// on_user_login(online, login)
typedef void (*shim_on_user_login_fn)(int, int, void*);
static shim_on_user_login_fn g_user_login_cb = nullptr;
static void* g_user_login_cb_ctx = nullptr;

int bambu_shim_set_on_user_login_fn(
    void* agent, shim_on_user_login_fn cb, void* ctx
) {
    if (!fp_set_user_login_cb) return -1;
    g_user_login_cb = cb;
    g_user_login_cb_ctx = ctx;
    OnUserLoginFn wrapper = [](int online, bool login) {
        if (g_user_login_cb) g_user_login_cb(online, login ? 1 : 0, g_user_login_cb_ctx);
    };
    return fp_set_user_login_cb(agent, wrapper);
}

// on_http_error(code, body)
typedef void (*shim_on_http_error_fn)(unsigned, const char*, void*);
static shim_on_http_error_fn g_http_err_cb = nullptr;
static void* g_http_err_cb_ctx = nullptr;

int bambu_shim_set_on_http_error_fn(
    void* agent, shim_on_http_error_fn cb, void* ctx
) {
    if (!fp_set_http_err_cb) return -1;
    g_http_err_cb = cb;
    g_http_err_cb_ctx = ctx;
    OnHttpErrorFn wrapper = [](unsigned code, std::string body) {
        if (g_http_err_cb) g_http_err_cb(code, body.c_str(), g_http_err_cb_ctx);
    };
    return fp_set_http_err_cb(agent, wrapper);
}

// get_country_code — simplified: just store a string
static std::string g_country_code = "US";

int bambu_shim_set_get_country_code_fn(void* agent, const char* code) {
    if (!fp_set_country_cb) return -1;
    g_country_code = std::string(code);
    GetCountryCodeFn wrapper = []() -> std::string {
        return g_country_code;
    };
    return fp_set_country_cb(agent, wrapper);
}

// on_subscribe_failure(topic)
typedef void (*shim_on_subscribe_failure_fn)(const char*, void*);
static shim_on_subscribe_failure_fn g_sub_fail_cb = nullptr;
static void* g_sub_fail_cb_ctx = nullptr;

int bambu_shim_set_on_subscribe_failure_fn(
    void* agent, shim_on_subscribe_failure_fn cb, void* ctx
) {
    if (!fp_set_sub_fail_cb) return -1;
    g_sub_fail_cb = cb;
    g_sub_fail_cb_ctx = ctx;
    GetSubscribeFailureFn wrapper = [](std::string topic) {
        if (g_sub_fail_cb) g_sub_fail_cb(topic.c_str(), g_sub_fail_cb_ctx);
    };
    return fp_set_sub_fail_cb(agent, wrapper);
}

// ---------------------------------------------------------------------------
// Upload cancellation
//
// Global atomic flag checked by WasCancelledFn during file transfer.
// The Rust side calls request_cancel() from the /cancel endpoint and
// reset_cancel() before each new print. This follows the same global
// pattern used by the callback setters above (single-agent constraint).
// ---------------------------------------------------------------------------

static std::atomic<bool> g_cancel_requested{false};

void bambu_shim_request_cancel() {
    g_cancel_requested.store(true, std::memory_order_seq_cst);
}

void bambu_shim_reset_cancel() {
    g_cancel_requested.store(false, std::memory_order_seq_cst);
}

// ---------------------------------------------------------------------------
// Print support
// ---------------------------------------------------------------------------

// C-compatible PrintParams (all const char* instead of std::string)
struct BambuShimPrintParams {
    const char* dev_id;
    const char* task_name;
    const char* project_name;
    const char* preset_name;
    const char* filename;
    const char* config_filename;
    int         plate_index;
    const char* ftp_folder;
    const char* ams_mapping;
    const char* ams_mapping2;
    const char* ams_mapping_info;
    const char* connection_type;
    const char* print_type;
    int         task_bed_leveling;
    int         task_flow_cali;
    int         task_vibration_cali;
    int         task_layer_inspect;
    int         task_record_timelapse;
    int         task_use_ams;
    const char* task_bed_type;
};

// Print progress callback: (stage, code, msg, ctx)
typedef void (*shim_on_print_progress_fn)(int, int, const char*, void*);

// Result struct filled by start_print
struct BambuShimPrintResult {
    int return_code;    // from fp_start_print
    int print_result;   // from the completion callback (-999 = never fired)
    int finished;       // 1 if completion callback fired
};

int bambu_shim_start_print(
    void* agent,
    const BambuShimPrintParams* p,
    shim_on_print_progress_fn progress_cb,
    void* progress_ctx,
    BambuShimPrintResult* result
) {
    if (!fp_start_print) return -1;

    // Convert C params to C++ PrintParams
    PrintParams params;
    params.dev_id            = p->dev_id ? p->dev_id : "";
    params.task_name         = p->task_name ? p->task_name : "";
    params.project_name      = p->project_name ? p->project_name : "";
    params.preset_name       = p->preset_name ? p->preset_name : "";
    params.filename          = p->filename ? p->filename : "";
    params.config_filename   = p->config_filename ? p->config_filename : "";
    params.plate_index       = p->plate_index;
    params.ftp_folder        = p->ftp_folder ? p->ftp_folder : "sdcard/";
    params.ftp_file          = "";
    params.ftp_file_md5      = "";
    params.nozzle_mapping    = "[]";
    params.ams_mapping       = p->ams_mapping ? p->ams_mapping : "[0,1,2,3]";
    params.ams_mapping2      = p->ams_mapping2 ? p->ams_mapping2 : "";
    params.ams_mapping_info  = p->ams_mapping_info ? p->ams_mapping_info : "";
    params.nozzles_info      = "";
    params.connection_type   = p->connection_type ? p->connection_type : "cloud";
    params.comments          = "";
    params.origin_profile_id = 0;
    params.stl_design_id     = 0;
    params.origin_model_id   = "";
    params.print_type        = p->print_type ? p->print_type : "from_normal";
    params.dst_file          = "";
    params.dev_name          = "";
    params.dev_ip            = "";
    params.use_ssl_for_ftp   = false;
    params.use_ssl_for_mqtt  = true;
    params.username          = "";
    params.password          = "";
    params.task_bed_leveling     = p->task_bed_leveling != 0;
    params.task_flow_cali        = p->task_flow_cali != 0;
    params.task_vibration_cali   = p->task_vibration_cali != 0;
    params.task_layer_inspect    = p->task_layer_inspect != 0;
    params.task_record_timelapse = p->task_record_timelapse != 0;
    params.task_use_ams          = p->task_use_ams != 0;
    params.task_bed_type         = p->task_bed_type ? p->task_bed_type : "auto";
    params.extra_options         = "";
    params.auto_bed_leveling     = 0;
    params.auto_flow_cali        = 0;
    params.auto_offset_cali      = 0;
    params.extruder_cali_manual_mode = -1;
    params.task_ext_change_assist = false;
    params.try_emmc_print         = false;

    // Track result
    result->print_result = -999;
    result->finished = 0;

    OnUpdateStatusFn update_fn = [progress_cb, progress_ctx, result](
        int status, int code, std::string msg
    ) {
        if (progress_cb)
            progress_cb(status, code, msg.c_str(), progress_ctx);
        if (status == 6) { result->print_result = 0; result->finished = 1; }
        else if (status == 7) { result->print_result = code; result->finished = 1; }
    };

    WasCancelledFn cancel_fn = []() -> bool {
        return g_cancel_requested.load(std::memory_order_seq_cst);
    };
    OnWaitFn wait_fn = [](int, std::string) -> bool { return false; };

    result->return_code = fp_start_print(agent, params, update_fn, cancel_fn, wait_fn);
    return 0;
}

} // extern "C"
