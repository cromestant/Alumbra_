// NOTE: no Kotlin plugin is declared here, and none should be added.
// AGP 9.4.1 compiles Kotlin natively via its built-in kotlinc and registers
// its own `kotlin` extension — applying org.jetbrains.kotlin.android (by any
// mechanism: plugins DSL or apply()) fails with "extension 'kotlin' already
// registered". (Found the hard way while scaffolding, Oct 2026.)
plugins {
    alias(libs.plugins.android.application)
}

android {
    namespace = "ai.alumbra.app"
    compileSdk {
        version = release(37)
    }

    defaultConfig {
        applicationId = "ai.alumbra.app"
        minSdk = 26
        targetSdk = 37
        versionCode = 1
        versionName = "1.0.0"
        // Submission default: the VPS. Settings still overrides it
        // (LAN / Tailscale presets for the dev server).
        buildConfigField("String", "DEFAULT_BASE_URL",
            "\"https://alumbra.onthe1.app\"")
    }

    buildTypes {
        release {
            isMinifyEnabled = false
        }
    }
    buildFeatures {
        buildConfig = true
    }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
}

dependencies {
    implementation(libs.androidx.core.ktx)
    implementation(libs.androidx.appcompat)
    implementation(libs.androidx.lifecycle.runtime.ktx)

    // CameraX — video capture for the clip & ask flow.
    implementation(libs.androidx.camera.core)
    implementation(libs.androidx.camera.camera2)
    implementation(libs.androidx.camera.lifecycle)
    implementation(libs.androidx.camera.video)
    implementation(libs.androidx.camera.view)
    // ExtensionsManager for the first-launch capability probe (Night/HDR).
    // Flagged per brief: second declared dep after OkHttp, same CameraX set.
    implementation(libs.androidx.camera.extensions)

    // HTTP client for POST /ask (multipart + long timeouts).
    implementation(libs.squareup.okhttp)
}
