package ai.alumbra.app

import android.content.Context
import android.hardware.camera2.CameraCharacteristics
import android.hardware.camera2.CameraManager
import android.util.Log
import androidx.camera.extensions.ExtensionsManager
import androidx.camera.lifecycle.ProcessCameraProvider
import java.util.concurrent.TimeUnit

/**
 * First-launch capability probe. Queries the back camera and persists a
 * one-line tier summary (shown in Settings → Camera tier). The tiered
 * second-look capture that consumes this is a later brief; v1 logs and
 * displays only.
 */
object CapabilityProbe {
    private const val TAG = "AlumbraProbe"

    fun probe(context: Context, done: (String) -> Unit) {
        Thread {
            val summary = runProbe(context.applicationContext)
            Prefs.setCameraTier(context, summary)
            Prefs.setProbed(context)
            Log.i(TAG, "Camera tier: $summary")
            done(summary)
        }.start()
    }

    private fun runProbe(context: Context): String {
        val parts = mutableListOf<String>()
        try {
            val manager = context.getSystemService(Context.CAMERA_SERVICE)
                as CameraManager
            val backId = manager.cameraIdList.firstOrNull { id ->
                val chars = manager.getCameraCharacteristics(id)
                chars.get(CameraCharacteristics.LENS_FACING) ==
                    CameraCharacteristics.LENS_FACING_BACK
            }
            if (backId == null) {
                return "No back camera found"
            }
            val chars = manager.getCameraCharacteristics(backId)
            val level = when (
                chars.get(CameraCharacteristics.INFO_SUPPORTED_HARDWARE_LEVEL)
            ) {
                CameraCharacteristics.INFO_SUPPORTED_HARDWARE_LEVEL_3 ->
                    "LEVEL_3"
                CameraCharacteristics.INFO_SUPPORTED_HARDWARE_LEVEL_FULL ->
                    "FULL"
                CameraCharacteristics.INFO_SUPPORTED_HARDWARE_LEVEL_LIMITED ->
                    "LIMITED"
                CameraCharacteristics.INFO_SUPPORTED_HARDWARE_LEVEL_LEGACY ->
                    "LEGACY"
                else -> "UNKNOWN"
            }
            parts.add(level)
            val caps = chars.get(
                CameraCharacteristics.REQUEST_AVAILABLE_CAPABILITIES
            )?.toSet().orEmpty()
            val raw = android.hardware.camera2.CameraMetadata
                .REQUEST_AVAILABLE_CAPABILITIES_RAW in caps
            val manual = android.hardware.camera2.CameraMetadata
                .REQUEST_AVAILABLE_CAPABILITIES_MANUAL_SENSOR in caps
            parts.add("RAW:" + if (raw) "yes" else "no")
            parts.add("MANUAL:" + if (manual) "yes" else "no")
            parts.add(
                "flash:" + if (
                    chars.get(CameraCharacteristics.FLASH_INFO_AVAILABLE)
                        == true
                ) "yes" else "no"
            )
        } catch (e: Exception) {
            parts.add("camera2:unavailable(${e.javaClass.simpleName})")
        }

        // CameraX extensions (Night / HDR) via ExtensionsManager.
        try {
            val providerFuture = ProcessCameraProvider.getInstance(context)
            val provider = providerFuture.get(15, TimeUnit.SECONDS)
            val extFuture = ExtensionsManager.getInstanceAsync(
                context, provider
            )
            val ext = extFuture.get(15, TimeUnit.SECONDS)
            val selector =
                androidx.camera.core.CameraSelector.DEFAULT_BACK_CAMERA
            val night = ext.isExtensionAvailable(
                selector, androidx.camera.extensions.ExtensionMode.NIGHT
            )
            val hdr = ext.isExtensionAvailable(
                selector, androidx.camera.extensions.ExtensionMode.HDR
            )
            parts.add("Night:" + if (night) "yes" else "no")
            parts.add("HDR:" + if (hdr) "yes" else "no")
        } catch (e: Exception) {
            parts.add("extensions:unknown(${e.javaClass.simpleName})")
        }
        return parts.joinToString(" · ")
    }
}
