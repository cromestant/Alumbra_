package ai.alumbra.app

import android.graphics.Typeface
import android.os.Bundle
import android.text.SpannableString
import android.text.Spanned
import android.text.style.ForegroundColorSpan
import android.text.style.StyleSpan
import android.util.TypedValue
import android.view.View
import android.widget.Button
import android.widget.LinearLayout
import android.widget.TextView
import androidx.appcompat.app.AppCompatActivity

/**
 * Answer screen: very large type, auto-spoken on arrival, What-I-saw
 * grounding list, and the "getting help" escalation state (verbatim line,
 * descriptive answer kept visible).
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

        tts = AlumbraTts(this)
        val statusView: TextView = findViewById(R.id.answer_status)
        val answerView: TextView = findViewById(R.id.answer_text)
        val sawButton: Button = findViewById(R.id.what_i_saw_button)
        val sawList: LinearLayout = findViewById(R.id.what_i_saw_list)

        val record = HistoryStore.get(
            this, intent.getLongExtra(EXTRA_RECORD_ID, -1)
        )
        if (record == null || (record.answer == null && record.error == null)) {
            statusView.text = getString(R.string.answer_missing)
            return
        }

        if (record.error != null && record.answer == null) {
            statusView.text = getString(R.string.answer_errored)
            answerView.text = record.error
            answer = record.error
            errored = true
        } else {
            // The asked question, as the server heard it, above the answer.
            val questionView: TextView = findViewById(R.id.question_text)
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
            answer = record.answer.orEmpty()
            escalated = record.behavior == "ESCALATE"
            statusView.text = if (escalated) {
                getString(R.string.getting_help)
            } else {
                record.question?.let { "Q: $it" }
                    ?: getString(R.string.answer_ready)
            }
            answerView.text = styledAnswer(answer)
            val entries = HistoryStore.sceneEntries(record)
            if (entries.isEmpty()) {
                sawButton.visibility = View.GONE
            } else {
                for (e in entries) {
                    val row = TextView(this).apply {
                        text = "${e.name} — ${e.clock} o'clock"
                        setTextSize(TypedValue.COMPLEX_UNIT_SP, 20f)
                    }
                    sawList.addView(row)
                }
            }
        }

        sawButton.setOnClickListener {
            val show = sawList.visibility != View.VISIBLE
            sawList.visibility = if (show) View.VISIBLE else View.GONE
            sawButton.text = getString(
                if (show) R.string.what_i_saw_hide else R.string.what_i_saw_show
            )
            Haptics.pulse(this)
        }
        findViewById<Button>(R.id.answer_replay_button).setOnClickListener {
            Haptics.pulse(this)
            replayAnswer()
        }
        findViewById<Button>(R.id.follow_up_button).setOnClickListener {
            finish()
        }

        Haptics.pulse(this)
        replayAnswer()
    }

    override fun onDestroy() {
        tts.shutdown()
        super.onDestroy()
    }

    private fun replayAnswer() {
        // Spoken as written: warnings already lead in the backend's text.
        // No silent fallback: the question preamble always precedes the
        // answer (escalation line first when escalated).
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

    /** "Warning:" lines in the warning style, original order preserved. */
    private fun styledAnswer(text: String): CharSequence {
        val out = SpannableString(text)
        val typed = TypedValue()
        theme.resolveAttribute(R.attr.warningTextColor, typed, true)
        val color = if (typed.resourceId != 0) {
            getColor(typed.resourceId)
        } else {
            typed.data
        }
        var index = 0
        for (line in text.split("\n")) {
            if (line.trimStart().startsWith("Warning:")) {
                out.setSpan(
                    ForegroundColorSpan(color), index, index + line.length,
                    Spanned.SPAN_EXCLUSIVE_EXCLUSIVE
                )
                out.setSpan(
                    StyleSpan(Typeface.BOLD), index, index + line.length,
                    Spanned.SPAN_EXCLUSIVE_EXCLUSIVE
                )
            }
            index += line.length + 1
        }
        return out
    }

    companion object {
        const val EXTRA_RECORD_ID = "record_id"
    }
}
