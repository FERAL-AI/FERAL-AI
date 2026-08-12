package ai.feral.bridge

import kotlinx.coroutines.*
import kotlinx.serialization.json.*
import okhttp3.*
import java.util.concurrent.TimeUnit

/**
 * FERAL Brain WebSocket Client for Android.
 * Mirrors the iOS FeralBrainClient.swift architecture.
 *
 * Connects to ws://host:9090/v1/node?api_key=...
 * Registers as node_type: "phone", platform: "android"
 */
/**
 * Confirmation callback handed to delegates when the brain requests
 * approval for a tool/skill action. The delegate decides interactively
 * (e.g. user prompt) and calls back with `true`/`false`. The client
 * relays the answer back to the brain as a `confirmation_response`
 * frame mirroring iOS `FeralBrainClient.swift:482-494`.
 */
typealias ConfirmationResponder = (approved: Boolean) -> Unit

interface FeralBrainDelegate {
    fun brainDidConnect()
    fun brainDidRegister(sessionId: String) {}
    fun brainDidDisconnect(reason: String)
    fun brainDidReceiveText(text: String)
    fun brainDidReceiveSDUI(json: JsonObject)
    fun brainDidReceiveAudio(data: ByteArray, encoding: String, sampleRate: Int)
    fun brainDidRequestStopPlayback()
    fun brainDidReceiveTranscript(text: String, isPartial: Boolean)
    fun brainDidReceiveExecute(executor: String, action: String, args: JsonObject)
    fun brainDidProposeSkill(manifest: JsonObject, reason: String) {}
    fun brainRequestsConfirmation(action: String, tier: String, respond: ConfirmationResponder) {}
}

class FeralBrainClient(
    private val host: String,
    private val port: Int = 9090,
    private val apiKey: String,
    private val nodeId: String = "android-phone",
    private val delegate: FeralBrainDelegate? = null,
) {
    private var webSocket: WebSocket? = null
    private val client = OkHttpClient.Builder()
        .readTimeout(0, TimeUnit.MILLISECONDS)
        .build()
    private val scope = CoroutineScope(Dispatchers.IO + SupervisorJob())
    private val json = Json { ignoreUnknownKeys = true; isLenient = true }

    var isConnected: Boolean = false
        private set

    fun connect() {
        val url = "ws://$host:$port/v1/node"
        val request = Request.Builder()
            .url(url)
            .addHeader("Authorization", "Bearer $apiKey")
            .build()

        webSocket = client.newWebSocket(request, object : WebSocketListener() {
            override fun onOpen(webSocket: WebSocket, response: Response) {
                isConnected = true
                sendRegistration()
                sendVoiceConfig()
                delegate?.brainDidConnect()
            }

            override fun onMessage(webSocket: WebSocket, text: String) {
                scope.launch { handleMessage(text) }
            }

            override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
                isConnected = false
                delegate?.brainDidDisconnect(reason)
            }

            override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
                isConnected = false
                delegate?.brainDidDisconnect(t.message ?: "Connection failed")
                scheduleReconnect()
            }
        })
    }

    fun disconnect() {
        webSocket?.close(1000, "Client disconnect")
        isConnected = false
    }

    private fun sendRegistration() {
        val msg = buildJsonObject {
            put("type", "register")
            put("hop", "node")
            putJsonObject("payload") {
                put("node_id", nodeId)
                put("node_type", "phone")
                put("platform", "android")
                put("os_version", android.os.Build.VERSION.RELEASE)
                put("device_model", android.os.Build.MODEL)
                putJsonArray("capabilities") {
                    add("voice")
                    add("sensors")
                    add("gps")
                    add("camera")
                    add("wake_word")
                }
            }
        }
        sendJson(msg)
    }

    private fun sendVoiceConfig() {
        val msg = buildJsonObject {
            put("type", "voice_config")
            put("hop", "node")
            putJsonObject("payload") {
                put("node_id", nodeId)
                put("supports_realtime", true)
                put("mode", "auto")
                put("sample_rate", 24000)
                put("encoding", "pcm16")
            }
        }
        sendJson(msg)
    }

    fun sendAudioChunk(data: ByteArray, chunkIndex: Int, isFinal: Boolean) {
        val b64 = android.util.Base64.encodeToString(data, android.util.Base64.NO_WRAP)
        val msg = buildJsonObject {
            put("type", "audio_chunk")
            put("hop", "node")
            putJsonObject("payload") {
                put("data_b64", b64)
                put("chunk_index", chunkIndex)
                put("is_final", isFinal)
                put("encoding", "pcm16")
                put("sample_rate", 24000)
            }
        }
        sendJson(msg)
    }

    fun sendTextCommand(text: String) {
        val msg = buildJsonObject {
            put("type", "text_command")
            put("hop", "node")
            putJsonObject("payload") {
                put("text", text)
            }
        }
        sendJson(msg)
    }

    fun sendSensorTelemetry(
        heartRate: Int? = null,
        spo2: Int? = null,
        steps: Int? = null,
        temperature: Double? = null,
        batteryPct: Int? = null,
    ) {
        val msg = buildJsonObject {
            put("type", "sensor_telemetry")
            put("hop", "node")
            putJsonObject("payload") {
                heartRate?.let { put("heart_rate", it) }
                spo2?.let { put("spo2_pct", it) }
                steps?.let { put("step_count", it) }
                temperature?.let { put("skin_temp_c", it) }
                batteryPct?.let { put("battery_pct", it) }
            }
        }
        sendJson(msg)
    }

    fun sendGlassesStatus(
        glassesConnected: Boolean,
        glassesModel: String = "FERAL",
        glassesBattery: Int = 100,
    ) {
        val msg = buildJsonObject {
            put("type", "glasses_status")
            put("hop", "node")
            putJsonObject("payload") {
                put("connected", glassesConnected)
                put("model", glassesModel)
                put("battery_pct", glassesBattery)
            }
        }
        sendJson(msg)
    }

    /**
     * Batched sensor frame — mirrors iOS `sendBatchSensorData(_:)` at
     * `feral-nodes/ios-app/Sources/FeralBridge/FeralBrainClient.swift:277-289`. Use when
     * multiple readings are produced in the same tick (e.g. glasses
     * sync flushing N samples at once) to avoid the per-reading round
     * trip cost of `sendSensorTelemetry`.
     *
     * `readings` is the per-sensor map keyed by sensor id; the values
     * are arbitrary JSON dictionaries (heart_rate, spo2, temperature,
     * etc.) the brain ingests verbatim.
     */
    fun sendBatchSensorData(
        readings: Map<String, JsonObject>,
        source: String = "feral_glasses",
    ) {
        val msg = buildJsonObject {
            put("type", "sensor_batch")
            put("hop", "node")
            putJsonObject("payload") {
                put("node_id", nodeId)
                putJsonObject("readings") {
                    readings.forEach { (key, value) -> put(key, value) }
                }
                put("timestamp", isoNow())
                put("source", source)
            }
        }
        sendJson(msg)
    }

    /**
     * Camera frame — mirrors iOS `sendCameraFrame(base64:source:)` at
     * `feral-nodes/ios-app/Sources/FeralBridge/FeralBrainClient.swift:432-443`. The
     * payload uses `image_b64` (base64-encoded JPEG/PNG) and a `source`
     * label (`rear`, `front`, `glasses`, etc.) the brain uses for
     * spatial reasoning.
     */
    fun sendCameraFrame(imageB64: String, source: String = "rear") {
        val msg = buildJsonObject {
            put("type", "frame")
            put("hop", "node")
            putJsonObject("payload") {
                put("node_id", nodeId)
                put("image_b64", imageB64)
                put("source", source)
                put("timestamp", isoNow())
            }
        }
        sendJson(msg)
    }

    /**
     * Skill approval — mirrors iOS `approveSkill(skillId:)` /
     * `rejectSkill(skillId:)` at
     * `feral-nodes/ios-app/Sources/FeralBridge/FeralBrainClient.swift:526-548`. Sent in
     * response to a `skill_proposal` frame the brain pushed when the
     * orchestrator wanted to install a new skill mid-conversation.
     */
    fun sendSkillApproval(skillId: String, approved: Boolean) {
        val msg = buildJsonObject {
            put("type", "skill_approval")
            put("hop", "node")
            putJsonObject("payload") {
                put("skill_id", skillId)
                put("approved", approved)
            }
        }
        sendJson(msg)
    }

    fun approveSkill(skillId: String) = sendSkillApproval(skillId, true)

    fun rejectSkill(skillId: String) = sendSkillApproval(skillId, false)

    /**
     * Confirmation response — mirrors the inline response emitted by
     * iOS at `feral-nodes/ios-app/Sources/FeralBridge/FeralBrainClient.swift:610-624`
     * when the user resolves a `confirmation_required` prompt. Public
     * here too so callers without a `ConfirmationResponder` (e.g.
     * deferred UI flows) can reply asynchronously.
     */
    fun sendConfirmationResponse(action: String, approved: Boolean) {
        val msg = buildJsonObject {
            put("type", "confirmation_response")
            put("hop", "node")
            putJsonObject("payload") {
                put("action", action)
                put("approved", approved)
            }
        }
        sendJson(msg)
    }

    private fun handleMessage(text: String) {
        try {
            val obj = json.parseToJsonElement(text).jsonObject
            val type = obj["type"]?.jsonPrimitive?.contentOrNull ?: return
            val payload = obj["payload"]?.jsonObject ?: JsonObject(emptyMap())

            when (type) {
                "registered" -> {
                    // Top-level session_id, matching iOS at FeralBrainClient.swift:455-463.
                    val sessionId = obj["session_id"]?.jsonPrimitive?.contentOrNull ?: ""
                    delegate?.brainDidRegister(sessionId)
                }
                "text_response" -> {
                    val msg = payload["text"]?.jsonPrimitive?.contentOrNull ?: ""
                    delegate?.brainDidReceiveText(msg)
                }
                "sdui" -> delegate?.brainDidReceiveSDUI(payload)
                "skill_proposal" -> {
                    val manifest = payload["manifest"]?.jsonObject ?: JsonObject(emptyMap())
                    val reason = payload["reason"]?.jsonPrimitive?.contentOrNull ?: ""
                    delegate?.brainDidProposeSkill(manifest, reason)
                }
                "confirmation_required" -> {
                    val action = payload["action"]?.jsonPrimitive?.contentOrNull ?: ""
                    val tier = payload["tier"]?.jsonPrimitive?.contentOrNull ?: ""
                    delegate?.brainRequestsConfirmation(action, tier) { approved ->
                        sendConfirmationResponse(action, approved)
                    }
                }
                "audio_response" -> {
                    val dataB64 = payload["data_b64"]?.jsonPrimitive?.contentOrNull ?: return
                    val encoding = payload["encoding"]?.jsonPrimitive?.contentOrNull ?: "pcm16"
                    val sampleRate = payload["sample_rate"]?.jsonPrimitive?.intOrNull ?: 24000
                    val audioBytes = android.util.Base64.decode(dataB64, android.util.Base64.DEFAULT)
                    delegate?.brainDidReceiveAudio(audioBytes, encoding, sampleRate)
                }
                "speech_started" -> delegate?.brainDidRequestStopPlayback()
                "transcript" -> {
                    val transcript = payload["text"]?.jsonPrimitive?.contentOrNull ?: ""
                    val isPartial = payload["is_partial"]?.jsonPrimitive?.booleanOrNull ?: false
                    delegate?.brainDidReceiveTranscript(transcript, isPartial)
                }
                "execute" -> {
                    val executor = payload["executor"]?.jsonPrimitive?.contentOrNull ?: ""
                    val action = payload["action"]?.jsonPrimitive?.contentOrNull ?: ""
                    val args = payload["args"]?.jsonObject ?: JsonObject(emptyMap())
                    delegate?.brainDidReceiveExecute(executor, action, args)
                }
                "tts_chunk" -> {
                    val dataB64 = payload["data_b64"]?.jsonPrimitive?.contentOrNull ?: return
                    val encoding = payload["encoding"]?.jsonPrimitive?.contentOrNull ?: "mp3"
                    val audioBytes = android.util.Base64.decode(dataB64, android.util.Base64.DEFAULT)
                    delegate?.brainDidReceiveAudio(audioBytes, encoding, 16000)
                }
            }
        } catch (e: Exception) {
            // Ignore malformed messages
        }
    }

    private fun sendJson(json: JsonObject) {
        webSocket?.send(json.toString())
    }

    private fun scheduleReconnect() {
        scope.launch {
            delay(3000)
            if (!isConnected) connect()
        }
    }

    fun destroy() {
        disconnect()
        scope.cancel()
    }

    /**
     * ISO-8601 timestamp matching the iOS `ISO8601DateFormatter()`
     * output used by `sensor_batch` / `frame` payloads. Kept private
     * + side-effect-free so the same format ends up on both wires.
     */
    private fun isoNow(): String {
        return java.time.OffsetDateTime
            .now(java.time.ZoneOffset.UTC)
            .format(java.time.format.DateTimeFormatter.ISO_OFFSET_DATE_TIME)
    }
}
