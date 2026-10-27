package ai.alumbra.app

import android.content.Intent
import android.os.Bundle
import android.util.TypedValue
import android.view.View
import android.widget.Button
import android.widget.LinearLayout
import android.widget.TextView
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity

/**
 * Answer screen (mock look): warnbox extracts "Warning:" lines above the
 * answer, grounding renders as always-visible chips, and ESCALATE swaps
 * in the helpbox + Keep-waiting/Leave row. Speech order is unchanged:
 * warnings spoken as written, escalation line first, then the question
 * preamble, then the answer.
 */
class AnswerActivity : AppCompatActivity() {

    private lateinit var tts: AlumbraTts
    private var answer: String = ""
    private var escalated: Boolean = false
    private var errored: Boolean = false
    private var preamble: String? = null

    override fun onCreate(savedInstanceState: Bundle?) {
        Prefs.applyTheme(this)
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_answer)
        Ui.applyEdgeToEdge(this)

        tts = AlumbraTts(this)
        val answerView: TextView = findViewById(R.id.answer_text)
        // Dead engine: the answer is already on screen; tell TalkBack
        // the voice is gone so the silence is explained.
        tts.onVoiceUnavailable = {
            runOnUiThread {
                answerView.announceForAccessibility(Spoken.VOICE_UNAVAILABLE)
            }
        }
        val questionView: TextView = findViewById(R.id.question_text)
        val chips: FlowLayout = findViewById(R.id.saw_chips)
        Ui.styleAppName(findViewById(R.id.appname_text), "ANSWER")

        val record = HistoryStore.get(
            this, intent.getLongExtra(EXTRA_RECORD_ID, -1)
        )
        if (record == null || (record.answer == null && record.error == null)) {
            answerView.text = getString(R.string.answer_missing)
            answer = getString(R.string.answer_missing)
            errored = true
        } else if (record.error != null && record.answer == null) {
            // Detail view shows the code; the voice speaks plain words.
            answerView.text = record.error +
                record.errorDetail?.let { " ($it)" }.orEmpty()
            answer = record.error
            errored = true
        } else {
            answer = record.answer.orEmpty()
            escalated = record.behavior == "ESCALATE"
            if (record.question != null) {
                questionView.text = record.question
            } else {
                questionView.visibility = View.GONE
            }
            preamble = when {
                record.questionSource == "none" ->
                    Spoken.DIDNT_CATCH_QUESTION
                record.question != null ->
                    Spoken.youAsked(record.question)
                else -> null
            }

            // Warnbox: Warning: lines display first; speech keeps order.
            val lines = answer.split("\n")
            val warnings = lines.filter {
                it.trimStart().startsWith("Warning:")
            }
            val body = lines.filterNot {
                it.trimStart().startsWith("Warning:")
            }.joinToString("\n").trim()
            if (warnings.isNotEmpty()) {
                findViewById<LinearLayout>(R.id.warnbox).visibility =
                    View.VISIBLE
                findViewById<TextView>(R.id.warn_lines).text =
                    warnings.joinToString("\n")
            }
            if (escalated) {
                findViewById<LinearLayout>(R.id.helpbox).visibility =
                    View.VISIBLE
                findViewById<TextView>(R.id.help_quote).text =
                    "\"" + Spoken.ESCALATE_LINE + "\""
                answerView.text =
                    getString(R.string.describe_prefix) + body.ifEmpty {
                        answer
                    }
                answerView.setTextColor(Ui.dimColor(this))
                findViewById<LinearLayout>(R.id.btnrow_normal).visibility =
                    View.GONE
                findViewById<LinearLayout>(R.id.btnrow_escalated).visibility =
                    View.VISIBLE
            } else {
                answerView.text = body.ifEmpty { answer }
            }

            for (e in HistoryStore.sceneEntries(record)) {
                chips.addView(chipFor(e))
            }
        }

        findViewById<Button>(R.id.answer_replay_button).setOnClickListener {
            Haptics.pulse(this)
            replayAnswer()
        }
        findViewById<Button>(R.id.follow_up_button).setOnClickListener {
            // Follow-up re-asks on the same media (no recapture). Old
            // records predate ask ids — say so plainly instead.
            val askId = record?.askId
            if (askId.isNullOrEmpty()) {
                Haptics.pulse(this)
                tts.speak(Spoken.MEDIA_GONE)
                Toast.makeText(this, Spoken.MEDIA_GONE, Toast.LENGTH_SHORT)
                    .show()
            } else {
                startActivity(
                    Intent(this, MainActivity::class.java).putExtra(
                        MainActivity.EXTRA_FOLLOWUP_ASK_ID, askId
                    )
                )
            }
        }
        findViewById<Button>(R.id.keep_waiting_button).setOnClickListener {
            // v1: no volunteer channel yet — confirm we're still here.
            Haptics.pulse(this)
            tts.speak(getString(R.string.help_waiting))
        }
        findViewById<Button>(R.id.leave_button).setOnClickListener {
            finish()
        }

        Haptics.pulse(this)
        replayAnswer()
    }

    override fun onDestroy() {
        tts.shutdown()
        super.onDestroy()
    }

    private fun chipFor(entry: SceneEntry): TextView {
        val label = if (entry.sizeClass == "far") {
            "${entry.name} · far, ${entry.clock} o'clock"
        } else {
            "${entry.name} · ${entry.clock} o'clock"
        }
        return TextView(this).apply {
            text = label
            setTextSize(TypedValue.COMPLEX_UNIT_SP, 14f)
            setTextColor(Ui.dimColor(this@AnswerActivity))
            setBackgroundResource(R.drawable.chip)
            val pad = (10 * resources.displayMetrics.density).toInt()
            setPadding(pad, pad / 2, pad, pad / 2)
        }
    }

    private fun replayAnswer() {
        if (errored) {
            tts.speak(answer)
            return
        }
        val lead = preamble
        if (escalated) {
            tts.speak(Spoken.ESCALATE_LINE)
            if (lead != null) tts.speakQueued(lead)
            if (answer.isNotBlank()) tts.speakQueued(answer)
        } else if (lead != null) {
            tts.speak(lead)
            if (answer.isNotBlank()) tts.speakQueued(answer)
        } else {
            tts.speak(answer)
        }
    }

    companion object {
        const val EXTRA_RECORD_ID = "record_id"
    }
}
