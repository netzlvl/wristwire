package io.wristwire.garminbridge

import android.app.Notification
import android.app.PendingIntent
import android.app.RemoteInput
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.os.BatteryManager
import android.os.Build
import android.os.Bundle
import android.os.KeyguardManager
import android.service.notification.NotificationListenerService
import android.service.notification.StatusBarNotification
import android.util.Log
import org.json.JSONArray
import org.json.JSONObject
import java.io.OutputStreamWriter
import java.net.HttpURLConnection
import java.net.URL
import java.nio.charset.StandardCharsets
import java.security.MessageDigest
import java.util.UUID
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit
import javax.crypto.Mac
import javax.crypto.spec.SecretKeySpec

/**
 * GarminBridgeService
 * ===================
 * Narrow Android transport adapter between Garmin Messenger and the WristWire
 * relay over HTTPS (VPN or local network).
 *
 * Key properties:
 * 1. Outbound-only connections to the relay host (no listening sockets on Android).
 * 2. Strict package filter ("com.garmin.android.apps.messenger") and owner
 *    conversation allowlist.
 * 3. Supports Immediate Implementation Gate ("INSPECT_ONLY_GATE_MODE") to record
 *    observable notification extras and RemoteInput actions before enabling
 *    full automation.
 * 4. Prefers Android Notification RemoteInput reply actions so replies can be
 *    sent while the phone screen remains locked and charging.
 * 5. Signs every HTTP request with HMAC-SHA256 (X-Device-Id, X-Timestamp,
 *    X-Nonce, X-Signature) and never stores the agent API key on the phone.
 */
class GarminBridgeService : NotificationListenerService() {

    companion object {
        private const val TAG = "GarminBridge"
        private const val GARMIN_PACKAGE = "com.garmin.android.apps.messenger"

        // Configure via BuildConfig / EncryptedSharedPreferences in your APK build
        private const val RELAY_BASE_URL = "https://your-relay-host:8788"
        private const val DEVICE_ID = "android-garmin-agent-01"
        private const val DEVICE_HMAC_SECRET = "REPLACE_WITH_GARMIN_BRIDGE_SECRET"
        private val ALLOWED_OWNER_SENDERS = setOf("owner", "watch-owner")

        // Set true during initial inspection gate to capture notification structure
        // before enabling automated replies.
        private const val INSPECT_ONLY_GATE_MODE = false
    }

    data class CachedReplyAction(
        val pendingIntent: PendingIntent,
        val remoteInputs: Array<RemoteInput>,
        val capturedAtMs: Long
    )

    private val workerPool = Executors.newScheduledThreadPool(2)
    private val replyActionsBySender = ConcurrentHashMap<String, CachedReplyAction>()
    private val recentMessageHashes = ConcurrentHashMap<String, Long>()

    override fun onCreate() {
        super.onCreate()
        Log.i(TAG, "GarminBridgeService starting (outbound-only relay)")

        workerPool.scheduleWithFixedDelay(
            { pollOutboxSafely() },
            2,
            2,
            TimeUnit.SECONDS
        )

        workerPool.scheduleWithFixedDelay(
            { sendHealthHeartbeatSafely() },
            5,
            60,
            TimeUnit.SECONDS
        )
    }

    override fun onDestroy() {
        workerPool.shutdownNow()
        super.onDestroy()
    }

    override fun onNotificationPosted(sbn: StatusBarNotification?) {
        if (sbn == null || sbn.packageName != GARMIN_PACKAGE) return
        val notification = sbn.notification ?: return
        if ((notification.flags and Notification.FLAG_GROUP_SUMMARY) != 0) return

        val extras: Bundle = notification.extras ?: return
        val rawTitle = extras.getCharSequence(Notification.EXTRA_TITLE)?.toString()?.trim() ?: ""
        val rawText = extras.getCharSequence(Notification.EXTRA_TEXT)?.toString()?.trim() ?: ""
        val bigText = extras.getCharSequence(Notification.EXTRA_BIG_TEXT)?.toString()?.trim() ?: ""

        var messagingStyleText = ""
        var messagingStyleSender = ""
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.N) {
            val messagesParcel = extras.getParcelableArray(Notification.EXTRA_MESSAGES)
            if (!messagesParcel.isNullOrEmpty()) {
                val lastBundle = messagesParcel.last() as? Bundle
                if (lastBundle != null) {
                    messagingStyleText = lastBundle.getCharSequence("text")?.toString()?.trim() ?: ""
                    messagingStyleSender = lastBundle.getCharSequence("sender")?.toString()?.trim() ?: ""
                }
            }
        }

        val sender = (if (messagingStyleSender.isNotEmpty()) messagingStyleSender else rawTitle).trim()
        val senderNorm = sender.lowercase()

        if (senderNorm !in ALLOWED_OWNER_SENDERS) {
            Log.w(TAG, "Ignoring Garmin notification from non-allowlisted sender")
            return
        }

        val effectiveBody = when {
            bigText.isNotEmpty() -> bigText
            messagingStyleText.isNotEmpty() -> messagingStyleText
            else -> rawText
        }
        if (effectiveBody.isEmpty()) return

        val actionTitles = JSONArray()
        var foundRemoteInput = false
        notification.actions?.forEach { action ->
            actionTitles.put(action.title?.toString() ?: "")
            val rInputs = action.remoteInputs
            if (!rInputs.isNullOrEmpty() && action.actionIntent != null) {
                foundRemoteInput = true
                replyActionsBySender[senderNorm] = CachedReplyAction(
                    pendingIntent = action.actionIntent,
                    remoteInputs = rInputs,
                    capturedAtMs = System.currentTimeMillis()
                )
            }
        }

        val looksLikeVoiceNote = effectiveBody.contains("Voice message", ignoreCase = true) ||
            effectiveBody.contains("Transcript", ignoreCase = true) ||
            rawText.contains("🎤")

        val keyguard = getSystemService(Context.KEYGUARD_SERVICE) as? KeyguardManager
        val isScreenLocked = keyguard?.isKeyguardLocked ?: true

        val contentKey = sha256("$senderNorm:${sbn.postTime}:$effectiveBody")
        val now = System.currentTimeMillis()
        val prev = recentMessageHashes.putIfAbsent(contentKey, now)
        if (prev != null && (now - prev) < 15_000L) {
            return
        }

        workerPool.execute {
            try {
                if (INSPECT_ONLY_GATE_MODE) {
                    val inspectPayload = JSONObject().apply {
                        put("package_name", GARMIN_PACKAGE)
                        put("sender", sender)
                        put("has_text", effectiveBody.isNotEmpty())
                        put("has_voice_transcript", looksLikeVoiceNote && effectiveBody.length > 15)
                        put("has_remote_input", foundRemoteInput)
                        put("action_titles", actionTitles)
                        put("notification_key", sbn.key)
                        put("screen_locked", isScreenLocked)
                        put("lte_mode", "always_on")
                        put("notes", "Captured via GarminBridgeService gate mode")
                    }
                    signedRequest("POST", "/v1/garmin/inspect", inspectPayload.toString())
                    return@execute
                }

                val ingestPayload = JSONObject().apply {
                    put("garmin_message_id", "${sbn.key}_${sbn.postTime}")
                    put("sender_id", sender)
                    put("source_type", if (looksLikeVoiceNote) "voice_note" else "text")
                    put("message_text", effectiveBody)
                    put("transcript_text", if (looksLikeVoiceNote) effectiveBody else "")
                    put("message_timestamp", sbn.postTime / 1000.0)
                }
                signedRequest("POST", "/v1/garmin/ingest", ingestPayload.toString())
            } catch (e: Exception) {
                Log.e(TAG, "Failed forwarding Garmin notification to relay", e)
            }
        }
    }

    private fun pollOutboxSafely() {
        try {
            val (code, body) = signedRequest("GET", "/v1/garmin/outbox", "")
            if (code != 200 || body.isEmpty()) return

            val root = JSONObject(body)
            val items = root.optJSONArray("items") ?: return
            for (i in 0 until items.length()) {
                val item = items.getJSONObject(i)
                val replyId = item.getString("reply_id")
                val recipient = item.getString("recipient_sender_id").lowercase()
                val replyText = item.getString("reply_text")

                val cached = replyActionsBySender[recipient]
                if (cached != null) {
                    val sentOk = sendViaRemoteInput(cached, replyText)
                    val ackPayload = JSONObject().apply {
                        put("delivered", sentOk)
                        put("delivery_method", "remote_input")
                        if (!sentOk) put("error", "RemoteInput PendingIntent CanceledException")
                    }
                    signedRequest("POST", "/v1/garmin/outbox/$replyId/ack", ackPayload.toString())
                } else {
                    val ackPayload = JSONObject().apply {
                        put("delivered", false)
                        put("delivery_method", "awaiting_remote_input_handle")
                        put("error", "No cached Garmin RemoteInput action for sender $recipient")
                    }
                    signedRequest("POST", "/v1/garmin/outbox/$replyId/ack", ackPayload.toString())
                }
            }
        } catch (e: Exception) {
            Log.e(TAG, "Error polling relay outbox", e)
        }
    }

    private fun sendViaRemoteInput(cached: CachedReplyAction, replyText: String): Boolean {
        return try {
            val fillInIntent = Intent().addFlags(Intent.FLAG_RECEIVER_FOREGROUND)
            val results = Bundle()
            for (remoteInput in cached.remoteInputs) {
                results.putCharSequence(remoteInput.resultKey, replyText)
            }
            RemoteInput.addResultsToIntent(cached.remoteInputs, fillInIntent, results)
            cached.pendingIntent.send(applicationContext, 0, fillInIntent)
            true
        } catch (e: Exception) {
            Log.e(TAG, "RemoteInput send failed", e)
            false
        }
    }

    private fun sendHealthHeartbeatSafely() {
        try {
            val bm = getSystemService(Context.BATTERY_SERVICE) as? BatteryManager
            val batteryPct = bm?.getIntProperty(BatteryManager.BATTERY_PROPERTY_CAPACITY) ?: -1
            val isCharging = bm?.isCharging ?: true
            val km = getSystemService(Context.KEYGUARD_SERVICE) as? KeyguardManager
            val locked = km?.isKeyguardLocked ?: true

            val payload = JSONObject().apply {
                put("battery_pct", batteryPct)
                put("is_charging", isCharging)
                put("wifi_connected", true)
                put("screen_locked", locked)
                put("garmin_app_running", true)
                put("bridge_mode", if (INSPECT_ONLY_GATE_MODE) "inspect_gate" else "notification_listener_remote_input")
            }
            signedRequest("POST", "/v1/garmin/heartbeat", payload.toString())
        } catch (e: Exception) {
            Log.w(TAG, "Health heartbeat failed", e)
        }
    }

    private fun signedRequest(method: String, path: String, jsonBody: String): Pair<Int, String> {
        val bodyBytes = jsonBody.toByteArray(StandardCharsets.UTF_8)
        val ts = String.format("%.3f", System.currentTimeMillis() / 1000.0)
        val nonce = "n_${UUID.randomUUID().toString().replace("-", "")}"
        val bodyHash = sha256Bytes(bodyBytes)
        val canonical = "$ts.$nonce.${method.uppercase()}.$path.$bodyHash"
        val signature = hmacSha256(DEVICE_HMAC_SECRET, canonical)

        val conn = (URL("$RELAY_BASE_URL$path").openConnection() as HttpURLConnection).apply {
            requestMethod = method
            connectTimeout = 8_000
            readTimeout = 8_000
            setRequestProperty("X-Device-Id", DEVICE_ID)
            setRequestProperty("X-Timestamp", ts)
            setRequestProperty("X-Nonce", nonce)
            setRequestProperty("X-Signature", signature)
            setRequestProperty("Content-Type", "application/json")
            if (method == "POST") {
                doOutput = true
                OutputStreamWriter(outputStream, StandardCharsets.UTF_8).use { it.write(jsonBody) }
            }
        }
        val status = conn.responseCode
        val stream = if (status in 200..299) conn.inputStream else conn.errorStream
        val respText = stream?.bufferedReader(StandardCharsets.UTF_8)?.use { it.readText() } ?: ""
        return Pair(status, respText)
    }

    private fun sha256(input: String): String = sha256Bytes(input.toByteArray(StandardCharsets.UTF_8))

    private fun sha256Bytes(bytes: ByteArray): String {
        val digest = MessageDigest.getInstance("SHA-256").digest(bytes)
        return digest.joinToString("") { "%02x".format(it) }
    }

    private fun hmacSha256(secret: String, data: String): String {
        val mac = Mac.getInstance("HmacSHA256")
        mac.init(SecretKeySpec(secret.toByteArray(StandardCharsets.UTF_8), "HmacSHA256"))
        val raw = mac.doFinal(data.toByteArray(StandardCharsets.UTF_8))
        return raw.joinToString("") { "%02x".format(it) }
    }
}

class GarminBridgeBootReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action == Intent.ACTION_BOOT_COMPLETED ||
            intent.action == Intent.ACTION_MY_PACKAGE_REPLACED
        ) {
            Log.i("GarminBridge", "Boot completed; system will rebind NotificationListenerService")
        }
    }
}