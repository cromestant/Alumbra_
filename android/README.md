# Alumbra Android app

Native Android client (Kotlin + CameraX), minSdk 26. Built with the Dell
Android toolchain; same Gradle conventions as the 7pos-cloud app
(Kotlin DSL `.kts` files, version catalog in `gradle/libs.versions.toml`).

## Build

```bash
./gradlew assembleDebug
# APK: app/build/outputs/apk/debug/app-debug.apk
```

Requires JDK 17+ and the Android SDK. Point Gradle at the SDK with
`local.properties` (`sdk.dir=/path/to/Android/Sdk`) or `ANDROID_HOME`.
`local.properties` is gitignored — machine-specific.

## What's here

- `MainActivity` — single screen with a big hold-to-record button
  (WhatsApp-voice-note style: press, hold, release to send). The button
  carries a `contentDescription` from day one; recording start/stop pulses
  haptically and status changes are announced via a live-region TextView.
  CameraX `VideoCapture` wiring, clip upload to `POST /ask`, and
  TextToSpeech readout are TODOs for hackathon time — marked in the code.

## Permissions

`CAMERA` + `RECORD_AUDIO` (the clip's audio track carries the spoken
question), `INTERNET` (clip upload to the backend).
