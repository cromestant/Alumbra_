package ai.alumbra.app

import android.os.SystemClock
import android.util.Log
import java.io.File
import java.net.InetSocketAddress
import java.net.Proxy
import java.util.concurrent.TimeUnit
import okhttp3.Call
import okhttp3.Callback
import okhttp3.EventListener
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.MultipartBody
import okhttp3.OkHttpClient
import okhttp3.Protocol
import okhttp3.Request
import okhttp3.RequestBody.Companion.asRequestBody
import okhttp3.RequestBody.Companion.toRequestBody
import okhttp3.Response
import org.json.JSONArray
import org.json.JSONObject
import java.io.IOException

/** One scene_model entry. clock is 1-12. Unknown fields are ignored. */
data class SceneEntry(
    val name: String,
    val clock: Int,
    val sizeClass: String,
    val confidence: Double?,
)

data class AskResult(
    val answer: String,
    val behavior: String,
    val confidence: Double,
    val scene: List<SceneEntry>,
    val flow: String?,
    val questionUsed: String?,
    val questionSource: String?,
    val askId: String?,
    // Present only when the server opened a volunteer case (ESCALATE).
    val escalation: EscalationCase? = null,
    // Base64 MP3 of the full spoken script, when the server voiced it.
    val audio: String? = null,
)

/** A volunteer case to wait on: poll every pollS for up to windowS. */
data class EscalationCase(val id: String, val pollS: Int, val windowS: Int)

/** One poll of a case. reply/volunteer only when status == "answered". */
data class EscalationStatus(
    val status: String,
    val volunteer: String?,
    val reply: String?,
)

sealed interface AskOutcome {
    data class Ok(val result: AskResult) : AskOutcome
    /**
     * message is plain words for the status line + voice; detail carries
     * the technical cause for History and logs, never spoken or shown.
     * expired marks a gone follow-up target (HTTP 410): the app says so
     * plainly and offers a fresh ask instead of retrying. plain marks a
     * message spoken as-is (no "Sorry, something went wrong." wrap).
     */
    data class Err(
        val message: String,
        val detail: String? = null,
        val expired: Boolean = false,
        val plain: Boolean = false,
    ) : AskOutcome
}

/**
 * POST {base_url}/ask as multipart: image (required JPEG), question
 * (optional text), audio + media (optional files), pitch_deg + focal_px
 * (optional capture geometry, both or neither). POST
 * {base_url}/followup re-runs the pipeline on a cached ask's media with
 * a new question (no image). Unknown response fields are ignored.
 */
class AskApi(baseUrl: String) {
    private val url = baseUrl.trimEnd('/') + "/ask"
    private val followupUrl = baseUrl.trimEnd('/') + "/followup"
    private val escalationsUrl = baseUrl.trimEnd('/') + "/v1/escalations"

    // Eval asks took 8-67s end to end; the Working state is built for it.
    private val client = OkHttpClient.Builder()
        .connectTimeout(10, TimeUnit.SECONDS)
        .readTimeout(180, TimeUnit.SECONDS)
        .writeTimeout(180, TimeUnit.SECONDS)
        .eventListener(NetEvents())
        .build()

    // Volunteer-case polls are small and frequent: short timeouts.
    private val pollClient = client.newBuilder()
        .readTimeout(10, TimeUnit.SECONDS)
        .writeTimeout(10, TimeUnit.SECONDS)
        .build()

    @Volatile
    private var call: Call? = null

    fun ask(
        image: File,
        question: String?,
        audio: File?,
        media: File?,
        meta: CaptureMeta? = null,
        callback: (AskOutcome) -> Unit,
    ) {
        val jpeg = "image/jpeg".toMediaType()
        val form = MultipartBody.Builder().setType(MultipartBody.FORM)
            .addFormDataPart("image", image.name, image.asRequestBody(jpeg))
        if (!question.isNullOrBlank()) {
            form.addFormDataPart("question", question)
        }
        if (meta != null) {
            // Capture geometry for metric distance; both or neither.
            form.addFormDataPart("pitch_deg", meta.pitchField())
            form.addFormDataPart("focal_px", meta.focalField())
        }
        if (audio != null && audio.exists()) {
            form.addFormDataPart(
                "audio", audio.name, audio.asRequestBody(
                    "audio/mp4".toMediaType()
                )
            )
        }
        if (media != null && media.exists()) {
            form.addFormDataPart(
                "media", media.name, media.asRequestBody(
                    "video/mp4".toMediaType()
                )
            )
        }
        postForm(
            url, form.build(),
            "image=${image.name}(${image.length()}B) " +
                "question=${!question.isNullOrBlank()} " +
                "audio=${audio?.length()} media=${media?.length()} " +
                "pitch_deg=${meta?.pitchField()} focal_px=${meta?.focalField()}",
            mapGone = false,
            callback = callback,
        )
    }

    /**
     * Follow-up on a cached ask: same media server-side, new question,
     * no image. 410 means the ask expired out of the server cache.
     */
    fun followup(
        askId: String,
        question: String?,
        callback: (AskOutcome) -> Unit,
    ) {
        val form = MultipartBody.Builder().setType(MultipartBody.FORM)
            .addFormDataPart("ask_id", askId)
        if (!question.isNullOrBlank()) {
            form.addFormDataPart("question", question)
        }
        postForm(
            followupUrl, form.build(),
            "ask_id=${askId.take(8)} " +
                "question=${!question.isNullOrBlank()}",
            mapGone = true,
            callback = callback,
        )
    }

    private fun postForm(
        targetUrl: String,
        form: MultipartBody,
        desc: String,
        mapGone: Boolean,
        callback: (AskOutcome) -> Unit,
    ) {
        val request = Request.Builder().url(targetUrl).post(form).build()
        val newCall = client.newCall(request)
        call = newCall
        val startMs = SystemClock.elapsedRealtime()
        Log.i(NET_TAG, "POST $targetUrl $desc")
        newCall.enqueue(object : Callback {
            override fun onFailure(call: Call, e: IOException) {
                val elapsed = SystemClock.elapsedRealtime() - startMs
                Log.i(
                    NET_TAG,
                    "request failed after ${elapsed}ms " +
                        "(canceled=${call.isCanceled()}): " +
                        "${e.javaClass.name}: ${e.message}",
                )
                if (call.isCanceled()) {
                    callback(AskOutcome.Err("Cancelled."))
                } else {
                    callback(
                        AskOutcome.Err(
                            Spoken.SERVER_UNREACHABLE,
                            "${e.javaClass.name}: ${e.message}",
                        )
                    )
                }
            }

            override fun onResponse(call: Call, response: Response) {
                response.use {
                    val elapsed = SystemClock.elapsedRealtime() - startMs
                    if (!it.isSuccessful) {
                        Log.i(
                            NET_TAG,
                            "request failed after ${elapsed}ms: " +
                                "HTTP ${it.code}",
                        )
                        if (mapGone && it.code == 410) {
                            callback(
                                AskOutcome.Err(
                                    Spoken.ASK_EXPIRED,
                                    "HTTP 410",
                                    expired = true,
                                )
                            )
                        } else if (it.code == 503 || it.code == 502) {
                            // Failure rule: a stage did not run (503) or
                            // the model API was down (502). Retry-able.
                            callback(
                                AskOutcome.Err(
                                    Spoken.TRY_AGAIN,
                                    "HTTP ${it.code}: " +
                                        it.body?.string().orEmpty().take(300),
                                    plain = true,
                                )
                            )
                        } else {
                            callback(
                                AskOutcome.Err(
                                    Spoken.SERVER_ERROR,
                                    "HTTP ${it.code}",
                                )
                            )
                        }
                        return
                    }
                    Log.i(
                        NET_TAG,
                        "response HTTP ${it.code} after ${elapsed}ms",
                    )
                    val text = it.body?.string().orEmpty()
                    callback(parse(text))
                }
            }
        })
    }

    /**
     * GET {base_url}/v1/escalations/{id}: the case's status. null on any
     * failure (network, 404) — the caller keeps polling to its window.
     */
    fun escalationStatus(id: String, callback: (EscalationStatus?) -> Unit) {
        val request = Request.Builder().url("$escalationsUrl/$id").get().build()
        pollClient.newCall(request).enqueue(object : Callback {
            override fun onFailure(call: Call, e: IOException) {
                Log.i(NET_TAG, "escalation poll failed: ${e.message}")
                callback(null)
            }

            override fun onResponse(call: Call, response: Response) {
                response.use {
                    if (!it.isSuccessful) {
                        Log.i(NET_TAG, "escalation poll HTTP ${it.code}")
                        callback(null)
                        return
                    }
                    callback(try {
                        val o = JSONObject(it.body?.string().orEmpty())
                        EscalationStatus(
                            o.optString("status", "pending"),
                            o.optString("volunteer", null)
                                .takeUnless { o.isNull("volunteer") },
                            o.optString("reply", null)
                                .takeUnless { o.isNull("reply") },
                        )
                    } catch (e: Exception) {
                        null
                    })
                }
            }
        })
    }

    /** POST .../cancel: the user stopped waiting. Fire and forget. */
    fun cancelEscalation(id: String) {
        val request = Request.Builder().url("$escalationsUrl/$id/cancel")
            .post(ByteArray(0).toRequestBody(null)).build()
        pollClient.newCall(request).enqueue(object : Callback {
            override fun onFailure(call: Call, e: IOException) {
                Log.i(NET_TAG, "escalation cancel failed: ${e.message}")
            }

            override fun onResponse(call: Call, response: Response) {
                Log.i(NET_TAG, "escalation cancel HTTP ${response.code}")
                response.close()
            }
        })
    }

    /**
     * Abandons the wait client-side. Note: the server may still finish
     * the ask; there is no server-side cancellation in v1.
     */
    fun cancel() {
        call?.cancel()
    }

    /** Connection-level events for the AlumbraNet logcat diagnosis. */
    private class NetEvents : EventListener() {
        override fun connectStart(
            call: Call,
            inetSocketAddress: InetSocketAddress,
            proxy: Proxy,
        ) {
            Log.i(NET_TAG, "connectStart $inetSocketAddress via $proxy")
        }

        override fun connectEnd(
            call: Call,
            inetSocketAddress: InetSocketAddress,
            proxy: Proxy,
            protocol: Protocol?,
        ) {
            Log.i(NET_TAG, "connectEnd $inetSocketAddress protocol=$protocol")
        }

        override fun connectFailed(
            call: Call,
            inetSocketAddress: InetSocketAddress,
            proxy: Proxy,
            protocol: Protocol?,
            e: IOException,
        ) {
            Log.i(
                NET_TAG,
                "connectFailed $inetSocketAddress: " +
                    "${e.javaClass.name}: ${e.message}",
            )
        }
    }

    companion object {
        private const val NET_TAG = "AlumbraNet"

        fun parse(text: String): AskOutcome {
            return try {
                val obj = JSONObject(text)
                val scene = mutableListOf<SceneEntry>()
                val arr: JSONArray = obj.optJSONArray("scene_model")
                    ?: JSONArray()
                for (i in 0 until arr.length()) {
                    val e = arr.optJSONObject(i) ?: continue
                    scene.add(
                        SceneEntry(
                            name = e.optString("name", "?"),
                            clock = e.optInt("clock", 12),
                            sizeClass = e.optString("size_class", ""),
                            confidence = e.optDouble("confidence", Double.NaN)
                                .takeUnless { it.isNaN() },
                        )
                    )
                }
                AskOutcome.Ok(
                    AskResult(
                        answer = obj.optString("answer", ""),
                        behavior = obj.optString("behavior", "ANSWER"),
                        confidence = obj.optDouble("confidence", 0.0),
                        scene = scene,
                        flow = obj.optString("flow", null),
                        questionUsed = obj.optString("question_used", null)
                            .takeUnless { obj.isNull("question_used") },
                        questionSource = obj.optString("question_source", null)
                            .takeUnless { obj.isNull("question_source") },
                        askId = obj.optString("ask_id", null)
                            .takeUnless { obj.isNull("ask_id") },
                        audio = obj.optString("audio", "")
                            .takeIf { it.isNotEmpty() },
                        escalation = obj.optJSONObject("escalation")?.let {
                            val id = it.optString("id", "")
                            if (id.isEmpty()) null else EscalationCase(
                                id,
                                it.optInt("poll_s", 3).coerceIn(1, 30),
                                it.optInt("window_s", 120).coerceIn(5, 900),
                            )
                        },
                    )
                )
            } catch (e: Exception) {
                Log.i(
                    NET_TAG,
                    "unparseable response: ${e.javaClass.name}: " +
                        e.message,
                )
                AskOutcome.Err(
                    Spoken.BAD_RESPONSE,
                    "${e.javaClass.name}: ${e.message}",
                )
            }
        }
    }
}
