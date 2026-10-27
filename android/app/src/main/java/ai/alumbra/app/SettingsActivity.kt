package ai.alumbra.app

import android.os.Bundle
import android.view.View
import android.widget.AdapterView
import android.widget.ArrayAdapter
import android.widget.Button
import android.widget.EditText
import android.widget.SeekBar
import android.widget.Spinner
import android.widget.TextView
import androidx.appcompat.widget.SwitchCompat
import androidx.appcompat.app.AppCompatActivity

/** Settings: language, speech rate, contrast, backend URL, camera tier. */
class SettingsActivity : AppCompatActivity() {

    override fun onCreate(savedInstanceState: Bundle?) {
        Prefs.applyTheme(this)
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_settings)
        Ui.applyEdgeToEdge(this)

        val spinner: Spinner = findViewById(R.id.language_spinner)
        spinner.adapter = ArrayAdapter(
            this, android.R.layout.simple_spinner_item,
            listOf("English", "Español"),
        ).also { it.setDropDownViewResource(android.R.layout.simple_spinner_dropdown_item) }
        spinner.setSelection(
            if (Prefs.language(this) == Prefs.LANG_SPANISH) 1 else 0
        )
        spinner.onItemSelectedListener = object : AdapterView.OnItemSelectedListener {
            override fun onItemSelected(
                parent: AdapterView<*>, view: View?, position: Int, id: Long
            ) {
                Prefs.setLanguage(
                    this@SettingsActivity,
                    if (position == 1) Prefs.LANG_SPANISH
                    else Prefs.LANG_ENGLISH,
                )
            }

            override fun onNothingSelected(parent: AdapterView<*>) = Unit
        }

        Ui.styleAppName(findViewById(R.id.appname_text), "SETTINGS")
        val rateValue: TextView = findViewById(R.id.rate_value)
        val seekbar: SeekBar = findViewById(R.id.rate_seekbar)
        fun showRate(progress: Int) {
            rateValue.text = if (progress == 10) {
                getString(R.string.rate_normal)
            } else {
                "%.2fx".format(0.5f + progress * 0.05f)
            }
        }
        seekbar.progress =
            ((Prefs.speechRate(this) - 0.5f) / 0.05f).toInt().coerceIn(0, 30)
        showRate(seekbar.progress)
        seekbar.setOnSeekBarChangeListener(object : SeekBar.OnSeekBarChangeListener {
            override fun onProgressChanged(
                bar: SeekBar, progress: Int, fromUser: Boolean
            ) {
                showRate(progress)
                if (fromUser) {
                    Prefs.setSpeechRate(
                        this@SettingsActivity, 0.5f + progress * 0.05f
                    )
                }
            }

            override fun onStartTrackingTouch(bar: SeekBar) = Unit
            override fun onStopTrackingTouch(bar: SeekBar) = Unit
        })

        val contrast: SwitchCompat = findViewById(R.id.contrast_switch)
        contrast.isChecked = Prefs.highContrast(this)
        contrast.setOnCheckedChangeListener { _, checked ->
            Prefs.setHighContrast(this, checked)
            recreate()
        }

        val urlField: EditText = findViewById(R.id.base_url_field)
        urlField.setText(Prefs.baseUrl(this))
        findViewById<Button>(R.id.preset_lan_button).setOnClickListener {
            urlField.setText(Prefs.DEFAULT_BASE_URL)
        }
        findViewById<Button>(R.id.preset_tail_button).setOnClickListener {
            urlField.setText(Prefs.TAILSCALE_BASE_URL)
        }
        findViewById<Button>(R.id.save_server_button).setOnClickListener {
            Prefs.setBaseUrl(this, urlField.text.toString())
            Haptics.pulse(this)
            finish()
        }

        findViewById<TextView>(R.id.camera_tier_text).text =
            Prefs.cameraTier(this)
    }
}
