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
        )
        records.add(0, record)
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
            )
        }
        file(context).writeText(arr.toString())
        return record
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
