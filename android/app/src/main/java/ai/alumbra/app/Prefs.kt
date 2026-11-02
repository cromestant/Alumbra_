package ai.alumbra.app

import android.app.Activity
import android.content.Context
import android.content.SharedPreferences

/** App settings, persisted in SharedPreferences. */
object Prefs {
    const val DEFAULT_BASE_URL = BuildConfig.DEFAULT_BASE_URL
    const val LAN_BASE_URL = "http://192.168.68.83:8123"
    const val TAILSCALE_BASE_URL = "http://100.100.139.0:8123"

    const val LANG_ENGLISH = "en"
    const val LANG_SPANISH = "es"

    private const val FILE = "alumbra"
    private const val KEY_BASE_URL = "base_url"
    private const val KEY_APP_TOKEN = "app_token"
    private const val KEY_LANGUAGE = "language"
    private const val KEY_SPEECH_RATE = "speech_rate"
    private const val KEY_HIGH_CONTRAST = "high_contrast"
    private const val KEY_CAMERA_TIER = "camera_tier"
    private const val KEY_PROBED = "capability_probed"

    private fun prefs(context: Context): SharedPreferences =
        context.getSharedPreferences(FILE, Context.MODE_PRIVATE)

    fun baseUrl(context: Context): String =
        prefs(context).getString(KEY_BASE_URL, DEFAULT_BASE_URL)
            ?: DEFAULT_BASE_URL

    fun setBaseUrl(context: Context, value: String) {
        prefs(context).edit().putString(KEY_BASE_URL, value.trim()).apply()
    }

    /** The server's APP_TOKEN, entered in Settings; empty sends none. */
    fun appToken(context: Context): String =
        prefs(context).getString(KEY_APP_TOKEN, "") ?: ""

    fun setAppToken(context: Context, value: String) {
        prefs(context).edit().putString(KEY_APP_TOKEN, value.trim()).apply()
    }

    fun language(context: Context): String =
        prefs(context).getString(KEY_LANGUAGE, LANG_ENGLISH) ?: LANG_ENGLISH

    fun setLanguage(context: Context, value: String) {
        prefs(context).edit().putString(KEY_LANGUAGE, value).apply()
    }

    fun speechRate(context: Context): Float =
        prefs(context).getFloat(KEY_SPEECH_RATE, 1.0f)

    fun setSpeechRate(context: Context, value: Float) {
        prefs(context).edit().putFloat(KEY_SPEECH_RATE, value).apply()
    }

    fun highContrast(context: Context): Boolean =
        prefs(context).getBoolean(KEY_HIGH_CONTRAST, true)

    fun setHighContrast(context: Context, value: Boolean) {
        prefs(context).edit().putBoolean(KEY_HIGH_CONTRAST, value).apply()
    }

    fun cameraTier(context: Context): String =
        prefs(context).getString(KEY_CAMERA_TIER, "Not probed yet")
            ?: "Not probed yet"

    fun setCameraTier(context: Context, value: String) {
        prefs(context).edit().putString(KEY_CAMERA_TIER, value).apply()
    }

    fun probed(context: Context): Boolean =
        prefs(context).getBoolean(KEY_PROBED, false)

    fun setProbed(context: Context) {
        prefs(context).edit().putBoolean(KEY_PROBED, true).apply()
    }

    /** Apply the contrast theme. Call before setContentView. */
    fun applyTheme(activity: Activity) {
        activity.setTheme(
            if (highContrast(activity)) R.style.Theme_Alumbra_HighContrast
            else R.style.Theme_Alumbra
        )
    }
}
