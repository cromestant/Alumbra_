package ai.alumbra.app

import android.content.Intent
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.os.SystemClock
import android.util.TypedValue
import android.util.Log
import android.view.View
import android.widget.Button
import android.widget.LinearLayout
import android.widget.TextView
import android.widget.Toast
import androidx.activity.OnBackPressedCallback
import androidx.appcompat.app.AppCompatActivity

/**
 * Answer screen (mock look): warnbox extracts "Warning:" lines above the
 * answer, grounding renders as always-visible chips, and ESCALATE swaps
 * in the helpbox + Keep-waiting/Leave row. Spoken script: escalation
 * line first (when escalated), then the answer, warnings as written.
 * The question is never echoed aloud — it is shown on screen and kept
 * in History. Only "I didn't catch a question" is spoken ahead of the
 * answer, because it carries information the answer does not.
 *
 * Waiting for a volunteer: a fresh ESCALATE that carries a server case
 * polls it (poll/window from the server) behind a big Stop waiting
 * button. A reply opens as its own VOLUNTEER record — labeled, and its
 * text is the attributed line (Spoken.volunteerSays), so no path shows
 * or speaks a reply as Alumbra's. No reply in the window: the fallback
 * line on the Ask screen. No case (kill switch, old server): exactly
 * the old Keep-waiting/Leave placeholder.
 *
 * "New question" (and system Back after a fresh or follow-up ask)
 * returns to the Ask screen; opened from History, Back returns there.
 */
class AnswerActivity : AppCompatActivity() {

    private lateinit var tts: AlumbraTts
    private var answer: String = ""
    private var escalated: Boolean = false
    private var errored: Boolean = false
    private var preamble: String? = null
    // The answer's server audio (full script), when it has one.
    private var audio: java.io.File? = null

    // Volunteer wait: caseId != null while waiting.
    private val handler = Handler(Looper.getMainLooper())
    private var api: AskApi? = null
    private var caseId: String? = null
    private var waitQuestion: String? = null
    private var pollMs = 3000L
    private var deadline = 0L
    private val pollTick = Runnable { poll() }

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
        audio = HistoryStore.audioOf(this, record)
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
            if (record.behavior == HistoryStore.BEHAVIOR_VOLUNTEER) {
                // A person answered: labeled as such; the conversation
                // continues as a new ask, not a follow-up.
                findViewById<LinearLayout>(R.id.volunteerbox).visibility =
                    View.VISIBLE
                findViewById<Button>(R.id.follow_up_button).visibility =
                    View.GONE
            }
            if (record.question != null) {
                questionView.text = record.question
            } else {
                questionView.visibility = View.GONE
            }
            preamble = if (record.questionSource == "none") {
                Spoken.DIDNT_CATCH_QUESTION
            } else {
                null
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

        findViewById<Button>(R.id.new_question_button).setOnClickListener {
            Haptics.pulse(this)
            goToAsk()
        }
        // Back after a fresh or follow-up ask lands on Ask (never on a
        // stale follow-up screen); from History it returns to History.
        val fromHistory = intent.getBooleanExtra(EXTRA_FROM_HISTORY, false)
        onBackPressedDispatcher.addCallback(
            this,
            object : OnBackPressedCallback(true) {
                override fun handleOnBackPressed() {
                    if (fromHistory) finish() else goToAsk()
                }
            },
        )
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
                    Intent(this, MainActivity::class.java)
                        .putExtra(MainActivity.EXTRA_FOLLOWUP_ASK_ID, askId)
                        .putExtra(
                            MainActivity.EXTRA_FOLLOWUP_QUESTION,
                            record.question,
                        )
                )
            }
        }
        findViewById<Button>(R.id.keep_waiting_button).setOnClickListener {
            // v1: no volunteer channel yet — confirm we're still here.
            Haptics.pulse(this)
            tts.speak(Spoken.HELP_WAITING)
        }
        findViewById<Button>(R.id.leave_button).setOnClickListener {
            goToAsk()
        }
        findViewById<Button>(R.id.stop_waiting_button).setOnClickListener {
            Haptics.pulse(this)
            endWaiting(cancel = true)
            goToAsk(Spoken.STOPPED_WAITING)
        }

        val resumeAt = savedInstanceState?.getLong(STATE_DEADLINE, 0L) ?: 0L
        Haptics.pulse(this)
        replayAnswer()
        val caseRecord = record?.takeIf {
            escalated && !fromHistory && !it.escalationId.isNullOrEmpty()
        }
        if (caseRecord != null &&
            (savedInstanceState == null || resumeAt > 0L)
        ) {
            startWaiting(caseRecord, resumeAt, announce = savedInstanceState == null)
        }
    }

    override fun onSaveInstanceState(outState: Bundle) {
        super.onSaveInstanceState(outState)
        // Rotation keeps the same wait (same case, same deadline).
        if (caseId != null) outState.putLong(STATE_DEADLINE, deadline)
    }

    override fun onDestroy() {
        handler.removeCallbacks(pollTick)
        // Leaving (New question, Back) stops the wait server-side too;
        // a configuration change resumes it instead.
        if (isFinishing) endWaiting(cancel = true)
        tts.shutdown()
        super.onDestroy()
    }

    /** Back to the root Ask screen, clearing answers above it. */
    private fun goToAsk(announce: String? = null) {
        tts.stop()
        startActivity(
            Intent(this, MainActivity::class.java).addFlags(
                Intent.FLAG_ACTIVITY_CLEAR_TOP or
                    Intent.FLAG_ACTIVITY_SINGLE_TOP
            ).apply {
                if (announce != null) {
                    putExtra(MainActivity.EXTRA_ANNOUNCE, announce)
                }
            }
        )
        finish()
    }

    private fun startWaiting(rec: HistoryRecord, resumeAt: Long, announce: Boolean) {
        caseId = rec.escalationId
        waitQuestion = rec.question
        pollMs = rec.escalationPollS * 1000L
        deadline = if (resumeAt > 0L) resumeAt else
            SystemClock.elapsedRealtime() + rec.escalationWindowS * 1000L
        api = AskApi(Prefs.baseUrl(this), Prefs.appToken(this))
        findViewById<LinearLayout>(R.id.btnrow_escalated).visibility =
            View.GONE
        findViewById<Button>(R.id.stop_waiting_button).visibility =
            View.VISIBLE
        showRemaining()
        // State speech, after the escalation line and the description.
        if (announce) tts.speakQueued(Spoken.WAITING_FOR_VOLUNTEER)
        Log.i(TAG, "waiting on case ${caseId?.take(8)} " +
            "window=${rec.escalationWindowS}s poll=${rec.escalationPollS}s")
        handler.postDelayed(pollTick, pollMs)
    }

    private fun showRemaining() {
        val left = ((deadline - SystemClock.elapsedRealtime()) / 1000L)
            .coerceAtLeast(0L).toInt()
        findViewById<TextView>(R.id.help_status).text =
            Spoken.waitingStatus(left)
    }

    private fun poll() {
        val id = caseId ?: return
        showRemaining()
        api?.escalationStatus(id) { st ->
            runOnUiThread { onStatus(id, st) }
        }
    }

    private fun onStatus(id: String, st: EscalationStatus?) {
        if (caseId != id || isFinishing || isDestroyed) return
        val reply = st?.reply
        if (st?.status == "answered" && !reply.isNullOrBlank()) {
            Log.i(TAG, "case ${id.take(8)} answered")
            deliver(st.volunteer.orEmpty(), reply)
            return
        }
        val over = SystemClock.elapsedRealtime() >= deadline
        if (over || st?.status == "expired" || st?.status == "cancelled") {
            // The server's window closes first (it opened the case before
            // answering), so a case past our deadline is already expired.
            Log.i(TAG, "case ${id.take(8)} closed: ${st?.status}")
            endWaiting(cancel = false)
            goToAsk(Spoken.NO_VOLUNTEER)
            return
        }
        handler.postDelayed(pollTick, pollMs)
    }

    /** The reply opens as its own VOLUNTEER record, which speaks itself. */
    private fun deliver(volunteer: String, reply: String) {
        endWaiting(cancel = false)
        val rec = HistoryStore.addVolunteer(this, waitQuestion, volunteer, reply)
        tts.stop()
        startActivity(
            Intent(this, AnswerActivity::class.java)
                .putExtra(EXTRA_RECORD_ID, rec.id)
        )
        finish()
    }

    private fun endWaiting(cancel: Boolean) {
        handler.removeCallbacks(pollTick)
        val id = caseId ?: return
        caseId = null
        if (cancel) api?.cancelEscalation(id)
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
        // The spoken script: escalation line, preamble, answer. The
        // server's audio is this same script in the product voice.
        val script = listOfNotNull(
            Spoken.ESCALATE_LINE.takeIf { escalated },
            preamble,
            answer.takeIf { it.isNotBlank() || (!escalated && preamble == null) },
        )
        val file = audio
        if (file != null) {
            tts.playFile(file, script)
            return
        }
        script.firstOrNull()?.let { tts.speak(it) }
        script.drop(1).forEach { tts.speakQueued(it) }
    }

    companion object {
        private const val TAG = "AlumbraEscalation"
        private const val STATE_DEADLINE = "wait_deadline"
        const val EXTRA_RECORD_ID = "record_id"
        const val EXTRA_FROM_HISTORY = "from_history"
    }
}
