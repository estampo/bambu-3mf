fn main() {
    cc::Build::new()
        .cpp(true)
        .std("c++17")
        .file("shim/shim.cpp")
        .compile("bambu_shim");

    // Link dl for dlopen/dlsym — only needed on Linux (macOS has it in libSystem)
    let target_os = std::env::var("CARGO_CFG_TARGET_OS").unwrap_or_default();
    if target_os == "linux" {
        println!("cargo:rustc-link-lib=dl");
    }
    println!("cargo:rustc-link-lib=pthread");
    if target_os == "macos" {
        println!("cargo:rustc-link-lib=c++");
    } else {
        println!("cargo:rustc-link-lib=stdc++");
    }
    println!("cargo:rerun-if-changed=shim/shim.cpp");
}
