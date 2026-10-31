package ai.alumbra.app

import android.content.Context
import org.json.JSONArray
import org.json.JSONObject
import java.io.File

/** One persisted ask. Unanswered/errored asks keep their error, honestly. */
data class HistoryRecord(
    val id: Long,
    val timestamp: Long,
    val question: String?,
    val questionSource: String?,
    val answer: String?,
    val behavior: String?,
    val confidence: Double?,
    val sceneJson: String,
    val error: String?,
    val errorDetail: String? = null,
    val askId: String? = null,
    // ESCALATE with a server case: what the Answer screen waits on.
    val escalationId: String? = null,
    val escalationPollS: Int = 3,
    val escalationWindowS: Int = 120,
    // behavior == VOLUNTEER: a person's answer. answer is then ALWAYS
    // the attributed line (Spoken.volunteerSays), never the bare reply.
    val volunteerName: String? = null,
    // The answer's server audio, a file in answerAudioDir; null -> TTS.
    val audioFile: String? = null,
)

/** Plain-JSON history in internal storage. Newest first. No new deps. */
object HistoryStore {
    private const val FILE = "history.json"
    private const val MAX_RECORDS = 200

    private fun file(context: Context): File =
        File(context.filesDir, FILE)

    fun add(
        context: Context,
        question: String?,
        result: AskResult?,
        error: String?,
        questionSource: String? = null,
        errorDetail: String? = null,
        askId: String? = result?.askId,
        audioFile: String? = null,
    ): HistoryRecord {
        val records = list(context).toMutableList()
        val sceneJson = result?.let {
            val arr = JSONArray()
            for (e in it.scene) {
                arr.put(
                    JSONObject()
                        .put("name", e.name)
                        .put("clock", e.clock)
                        .put("size_class", e.sizeClass)
                )
            }
            arr.toString()
        } ?: "[]"
        val record = HistoryRecord(
            id = System.currentTimeMillis(),
            timestamp = System.currentTimeMillis(),
            question = question,
            questionSource = questionSource,
            answer = result?.answer,
            behavior = result?.behavior,
            confidence = result?.confidence,
            sceneJson = sceneJson,
            error = error,
            errorDetail = errorDetail,
            askId = askId,
            escalationId = result?.escalation?.id,
            escalationPollS = result?.escalation?.pollS ?: 3,
            escalationWindowS = result?.escalation?.windowS ?: 120,
            audioFile = audioFile,
        )
        records.add(0, record)
        save(context, records)
        return record
    }

    /**
     * A volunteer's reply, stored as a human answer distinct from AI
     * answers (behavior VOLUNTEER). The stored text is the attributed
     * line, so every place that shows or speaks it carries the name.
     */
    fun addVolunteer(
        context: Context,
        question: String?,
        volunteer: String,
        reply: String,
    ): HistoryRecord {
        val records = list(context).toMutableList()
        val now = System.currentTimeMillis()
        val record = HistoryRecord(
            id = if (records.any { it.id == now }) now + 1 else now,
            timestamp = now,
            question = question,
            questionSource = null,
            answer = Spoken.volunteerSays(volunteer, reply),
            behavior = BEHAVIOR_VOLUNTEER,
            confidence = null,
            sceneJson = "[]",
            error = null,
            volunteerName = volunteer.trim(),
        )
        records.add(0, record)
        save(context, records)
        return record
    }

    const val BEHAVIOR_VOLUNTEER = "VOLUNTEER"

    private fun save(context: Context, records: List<HistoryRecord>) {
        val trimmed = records.take(MAX_RECORDS)
        val arr = JSONArray()
        for (r in trimmed) {
            arr.put(
                JSONObject()
                    .put("id", r.id)
                    .put("timestamp", r.timestamp)
                    .put("question", r.question ?: JSONObject.NULL)
                    .put("questionSource", r.questionSource ?: JSONObject.NULL)
                    .put("answer", r.answer ?: JSONObject.NULL)
                    .put("behavior", r.behavior ?: JSONObject.NULL)
                    .put("confidence", r.confidence ?: JSONObject.NULL)
                    .put("sceneJson", r.sceneJson)
                    .put("error", r.error ?: JSONObject.NULL)
                    .put("errorDetail", r.errorDetail ?: JSONObject.NULL)
                    .put("askId", r.askId ?: JSONObject.NULL)
                    .put("escalationId", r.escalationId ?: JSONObject.NULL)
                    .put("escalationPollS", r.escalationPollS)
                    .put("escalationWindowS", r.escalationWindowS)
                    .put("volunteerName", r.volunteerName ?: JSONObject.NULL)
                    .put("audioFile", r.audioFile ?: JSONObject.NULL)
            )
        }
        file(context).writeText(arr.toString())
        // Audio of records trimmed out of History goes with them.
        val kept = trimmed.mapNotNull { it.audioFile }.toSet()
        answerAudioDir(context).listFiles()?.forEach {
            if (it.name !in kept) it.delete()
        }
    }

    fun answerAudioDir(context: Context): File =
        File(context.filesDir, "answer_audio").apply { mkdirs() }

    /** The record's audio file when it is still on disk. */
    fun audioOf(context: Context, record: HistoryRecord?): File? =
        record?.audioFile?.let { File(answerAudioDir(context), it) }
            ?.takeIf { it.isFile && it.length() > 0 }

    /** Decode the response's base64 MP3 into a new file; null on failure. */
    fun saveAudio(context: Context, base64: String?): String? {
        if (base64.isNullOrEmpty()) return null
        return try {
            val bytes = android.util.Base64.decode(
                base64, android.util.Base64.DEFAULT
            )
            val name = "answer-${System.currentTimeMillis()}.mp3"
            File(answerAudioDir(context), name).writeBytes(bytes)
            name
        } catch (e: Exception) {
            null
        }
    }

    fun list(context: Context): List<HistoryRecord> {
        val f = file(context)
        if (!f.exists()) return emptyList()
        return try {
            val arr = JSONArray(f.readText())
            (0 until arr.length()).mapNotNull { i ->
                val o = arr.optJSONObject(i) ?: return@mapNotNull null
                HistoryRecord(
                    id = o.optLong("id", 0),
                    timestamp = o.optLong("timestamp", 0),
                    question = o.optString("question", null)
                        .takeUnless { o.isNull("question") },
                    questionSource = o.optString("questionSource", null)
                        .takeUnless { o.isNull("questionSource") },
                    answer = o.optString("answer", null)
                        .takeUnless { o.isNull("answer") },
                    behavior = o.optString("behavior", null)
                        .takeUnless { o.isNull("behavior") },
                    confidence = o.optDouble("confidence", Double.NaN)
                        .takeUnless { it.isNaN() },
                    sceneJson = o.optString("sceneJson", "[]"),
                    error = o.optString("error", null)
                        .takeUnless { o.isNull("error") },
                    errorDetail = o.optString("errorDetail", null)
                        .takeUnless { o.isNull("errorDetail") },
                    askId = o.optString("askId", null)
                        .takeUnless { o.isNull("askId") },
                    escalationId = o.optString("escalationId", null)
                        .takeUnless { o.isNull("escalationId") },
                    escalationPollS = o.optInt("escalationPollS", 3),
                    escalationWindowS = o.optInt("escalationWindowS", 120),
                    volunteerName = o.optString("volunteerName", null)
                        .takeUnless { o.isNull("volunteerName") },
                    audioFile = o.optString("audioFile", null)
                        .takeUnless { o.isNull("audioFile") },
                )
            }
        } catch (e: Exception) {
            emptyList()
        }
    }

    fun get(context: Context, id: Long): HistoryRecord? =
        list(context).firstOrNull { it.id == id }

    fun sceneEntries(record: HistoryRecord): List<SceneEntry> {
        return try {
            val arr = JSONArray(record.sceneJson)
            (0 until arr.length()).mapNotNull { i ->
                val e = arr.optJSONObject(i) ?: return@mapNotNull null
                SceneEntry(
                    name = e.optString("name", "?"),
                    clock = e.optInt("clock", 12),
                    sizeClass = e.optString("size_class", ""),
                    confidence = null,
                )
            }
        } catch (e: Exception) {
            emptyList()
        }
    }
}
