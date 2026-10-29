package ai.alumbra.app

import android.graphics.BitmapFactory
import android.hardware.camera2.CameraCharacteristics
import android.util.Log
import androidx.annotation.OptIn
import androidx.camera.camera2.interop.Camera2CameraInfo
import androidx.camera.camera2.interop.ExperimentalCamera2Interop
import androidx.camera.core.Camera
import androidx.camera.core.ResolutionInfo
import java.io.File
import java.util.Locale
import kotlin.math.asin
import kotlin.math.max
import kotlin.math.sqrt

/**
 * Capture metadata sent with a photo ask, so the backend can turn box
 * geometry into meters. Both values or neither: a missing one means the
 * backend answers exactly as for an old app.
 */
data class CaptureMeta(val pitchDeg: Double, val focalPx: Double) {
    fun pitchField(): String = String.format(Locale.ROOT, "%.2f", pitchDeg)
    fun focalField(): String = String.format(Locale.ROOT, "%.1f", focalPx)
}

object CaptureGeometry {

    /**
     * Degrees the back camera's optical axis points DOWN from horizontal:
     * level = 0, straight at the floor = 90, up at the sky negative.
     * [g] is a gravity (or resting accelerometer) vector in device
     * coordinates, which reads +9.8 along the axis pointing up. The back
     * camera looks along device -z, so the axis points down by
     * asin(g_z / |g|). Roll does not change it.
     */
    fun pitchDownDeg(g: FloatArray?): Double? {
        if (g == null || g.size < 3) return null
        val n = sqrt((g[0] * g[0] + g[1] * g[1] + g[2] * g[2]).toDouble())
        if (n < 1.0) return null
        return Math.toDegrees(asin((g[2] / n).coerceIn(-1.0, 1.0)))
    }

    /**
     * Pitch of a clip frame: the median of the gravity samples within
     * [windowNs] of [frameNs] (both on the elapsedRealtimeNanos clock),
     * so hand shake at that instant does not set it. Null without a
     * sample near the frame.
     */
    fun pitchAt(
        samples: List<Pair<Long, FloatArray>>,
        frameNs: Long,
        windowNs: Long = 150_000_000L,
    ): Double? {
        val near = samples.filter { kotlin.math.abs(it.first - frameNs) <= windowNs }
            .mapNotNull { pitchDownDeg(it.second) }
            .sorted()
        if (near.isEmpty()) return null
        return near[near.size / 2]
    }

    /**
     * Focal length in pixels of the uploaded JPEG (a photo, or a frame
     * taken from a clip: [res] is that use case's resolution info).
     *
     * The stream (ImageCapture beside VideoCapture HD, or the HD video) is
     * the largest centered crop of the sensor's active array with the
     * stream's aspect, scaled to the stream size, then narrowed by the
     * zoom ratio. CameraX may crop it further (cropRect) and the saved
     * JPEG may be rotated, never rescaled further: its long side is
     * compared with the crop's long side anyway. The focal length starts
     * from the lens's factory calibration (fx, active-array px) when the
     * device reports one, else focal_mm / sensor_width_mm x pixel-array
     * width. Principal point assumed at the image center.
     */
    @OptIn(ExperimentalCamera2Interop::class)
    fun focalPx(camera: Camera, res: ResolutionInfo?, jpeg: File): Double? {
        return try {
            val info = Camera2CameraInfo.from(camera.cameraInfo)
            val active = info.getCameraCharacteristic(
                CameraCharacteristics.SENSOR_INFO_ACTIVE_ARRAY_SIZE
            ) ?: return null
            val calib = info.getCameraCharacteristic(
                CameraCharacteristics.LENS_INTRINSIC_CALIBRATION
            )
            val focalMm = info.getCameraCharacteristic(
                CameraCharacteristics.LENS_INFO_AVAILABLE_FOCAL_LENGTHS
            )?.firstOrNull()
            val phys = info.getCameraCharacteristic(
                CameraCharacteristics.SENSOR_INFO_PHYSICAL_SIZE
            )
            val pixels = info.getCameraCharacteristic(
                CameraCharacteristics.SENSOR_INFO_PIXEL_ARRAY_SIZE
            )
            val fromSpec = if (focalMm != null && phys != null &&
                pixels != null && phys.width > 0f
            ) {
                focalMm / phys.width * pixels.width
            } else null
            val fromCalib = calib?.firstOrNull()?.takeIf { it > 0f }
            val fActive = (fromCalib ?: fromSpec)?.toDouble() ?: return null

            if (res == null) return null
            val stream = res.resolution  // sensor orientation, pre-rotation
            val streamAspect = stream.width.toDouble() / stream.height
            val activeAspect = active.width().toDouble() / active.height()
            val streamScale = if (streamAspect >= activeAspect) {
                stream.width.toDouble() / active.width()
            } else {
                stream.height.toDouble() / active.height()
            }
            val zoom = (camera.cameraInfo.zoomState.value?.zoomRatio ?: 1f)
                .toDouble()
            val crop = res.cropRect
            val cropLong = max(crop.width(), crop.height())
            val opts = BitmapFactory.Options().apply {
                inJustDecodeBounds = true
            }
            BitmapFactory.decodeFile(jpeg.path, opts)
            val jpegLong = max(opts.outWidth, opts.outHeight)
            val jpegScale = if (cropLong > 0 && jpegLong > 0) {
                jpegLong.toDouble() / cropLong
            } else 1.0
            val focal = fActive * streamScale * zoom * jpegScale
            Log.i(
                "Alumbra",
                String.format(
                    Locale.ROOT,
                    "focal_px=%.1f (f_active=%.1f %s; f_mm=%s phys=%s " +
                        "pixels=%s active=%dx%d stream=%dx%d scale=%.4f " +
                        "zoom=%.2f crop=%dx%d jpeg=%dx%d)",
                    focal, fActive,
                    if (fromCalib != null) "calibration" else "spec",
                    focalMm, phys, pixels,
                    active.width(), active.height(),
                    stream.width, stream.height, streamScale, zoom,
                    crop.width(), crop.height(),
                    opts.outWidth, opts.outHeight,
                )
            )
            focal.takeIf { it.isFinite() && it > 0 }
        } catch (e: Exception) {
            Log.w("Alumbra", "focal_px unavailable", e)
            null
        }
    }
}
