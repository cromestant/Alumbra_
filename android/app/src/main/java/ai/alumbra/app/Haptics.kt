package ai.alumbra.app

import android.content.Context
import android.os.Build
import android.os.VibrationEffect
import android.os.Vibrator
import android.os.VibratorManager

/** Haptic confirmation so no state is ever silent. */
object Haptics {
    fun pulse(context: Context, millis: Long = 80) {
        val vibrator: Vibrator = if (
            Build.VERSION.SDK_INT >= Build.VERSION_CODES.S
        ) {
            val manager = context.getSystemService(Context.VIBRATOR_MANAGER_SERVICE)
                as VibratorManager
            manager.defaultVibrator
        } else {
            @Suppress("DEPRECATION")
            context.getSystemService(Context.VIBRATOR_SERVICE) as Vibrator
        }
        vibrator.vibrate(
            VibrationEffect.createOneShot(
                millis, VibrationEffect.DEFAULT_AMPLITUDE
            )
        )
    }
}
