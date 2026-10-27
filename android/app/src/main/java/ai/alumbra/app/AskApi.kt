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
)

sealed interface AskOutcome {
    data class Ok(val result: AskResult) : AskOutcome
    /**
     * message is plain words for the status line + voice; detail carries
     * the technical cause for History and logs, never spoken or shown.
     */
    data class Err(val message: String, val detail: String? = null) :
        AskOutcome
}

/**
 * POST {base_url}/ask as multipart: image (required JPEG), question
 * (optional text), audio + media (optional files, ignored by the backend
 * today, consumed by later briefs). Unknown response fields are ignored.
 */
class AskApi(baseUrl: String) {
    private val url = baseUrl.trimEnd('/') + "/ask"

    // Eval asks took 8-67s end to end; the Working state is built for it.
    private val client = OkHttpClient.Builder()
        .connectTimeout(10, TimeUnit.SECONDS)
        .readTimeout(180, TimeUnit.SECONDS)
        .writeTimeout(180, TimeUnit.SECONDS)
        .eventListener(NetEvents())
        .build()

    @Volatile
    private var call: Call? = null

    fun ask(
        image: File,
        question: String?,
        audio: File?,
        media: File?,
        callback: (AskOutcome) -> Unit,
    ) {
        val jpeg = "image/jpeg".toMediaType()
        val form = MultipartBody.Builder().setType(MultipartBody.FORM)
            .addFormDataPart("image", image.name, image.asRequestBody(jpeg))
        if (!question.isNullOrBlank()) {
            form.addFormDataPart("question", question)
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
        val request = Request.Builder().url(url).post(form.build()).build()
        val newCall = client.newCall(request)
        call = newCall
        val startMs = SystemClock.elapsedRealtime()
        Log.i(
            NET_TAG,
            "POST $url image=${image.name}(${image.length()}B) " +
                "question=${!question.isNullOrBlank()} " +
                "audio=${audio?.length()} media=${media?.length()}",
        )
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
                        callback(
                            AskOutcome.Err(
                                Spoken.SERVER_ERROR,
                                "HTTP ${it.code}",
                            )
                        )
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
