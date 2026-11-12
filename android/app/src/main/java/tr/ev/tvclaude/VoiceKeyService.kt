package tr.ev.tvclaude

import android.accessibilityservice.AccessibilityService
import android.app.SearchManager
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Color
import android.graphics.PixelFormat
import android.graphics.drawable.GradientDrawable
import android.media.AudioAttributes
import android.media.AudioFocusRequest
import android.media.AudioManager
import android.media.MediaPlayer
import android.net.Uri
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.os.SystemClock
import android.speech.RecognitionListener
import android.speech.RecognizerIntent
import android.speech.SpeechRecognizer
import android.speech.tts.TextToSpeech
import android.util.Log
import android.util.TypedValue
import android.view.Gravity
import android.view.KeyEvent
import android.view.WindowManager
import android.view.accessibility.AccessibilityEvent
import android.widget.TextView
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.net.HttpURLConnection
import java.net.URL
import java.util.Locale
import java.util.concurrent.Executors

class VoiceKeyService : AccessibilityService() {

    private val main = Handler(Looper.getMainLooper())
    private val io = Executors.newSingleThreadExecutor()
    private var recognizer: SpeechRecognizer? = null
    private var tts: TextToSpeech? = null
    private var ttsReady = false
    private var overlay: TextView? = null
    private var listening = false
    private var waiting = false
    private var generation = 0
    private var player: MediaPlayer? = null
    private var focus: AudioFocusRequest? = null
    private var serverUrl = "http://192.168.1.55:8790"
    private var token = ""
    private var triggerKey = KeyEvent.KEYCODE_F8

    override fun onServiceConnected() {
        super.onServiceConnected()
        loadConfig()
        tts = TextToSpeech(this) { status ->
            if (status == TextToSpeech.SUCCESS) {
                tts?.language = Locale("tr", "TR")
                ttsReady = true
            }
        }
        recognizer = SpeechRecognizer.createSpeechRecognizer(this).also { it.setRecognitionListener(listener) }
        io.execute { sendApps() }
        Log.i(TAG, "hazır, tetik tuşu=${KeyEvent.keyCodeToString(triggerKey)} sunucu=$serverUrl")
    }

    private fun loadConfig() {
        val file = File(getExternalFilesDir(null), "config.json")
        if (!file.exists()) {
            Log.w(TAG, "config.json yok: ${file.path}")
            return
        }
        runCatching {
            val c = JSONObject(file.readText())
            serverUrl = c.optString("url", serverUrl).trimEnd('/')
            token = c.optString("token", token)
            triggerKey = c.optInt("key", triggerKey)
        }.onFailure { Log.e(TAG, "config okunamadı", it) }
    }

    override fun onKeyEvent(event: KeyEvent): Boolean {
        if (event.keyCode != triggerKey) return false
        if (event.action == KeyEvent.ACTION_DOWN && event.repeatCount == 0) {
            main.post { onTrigger() }
        }
        return true
    }

    // Tuş: konuşuyorsa sustur, yanıt bekliyorsa iptal et, dinliyorsa dinlemeyi kes, yoksa dinlemeye başla.
    private fun onTrigger() {
        when {
            isSpeaking() -> { stopSpeaking(); hideOverlay() }
            waiting -> { generation++; waiting = false; hideOverlay() }
            listening -> cancelListening()
            else -> startListening()
        }
    }

    private fun isSpeaking() = player != null || tts?.isSpeaking == true

    private fun stopSpeaking() {
        generation++
        tts?.stop()
        player?.let { runCatching { it.stop() }; it.release() }
        player = null
        releaseFocus()
    }

    private fun startListening() {
        loadConfig()
        stopSpeaking()
        val intent = Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH).apply {
            putExtra(RecognizerIntent.EXTRA_LANGUAGE_MODEL, RecognizerIntent.LANGUAGE_MODEL_FREE_FORM)
            putExtra(RecognizerIntent.EXTRA_LANGUAGE, "tr-TR")
            putExtra(RecognizerIntent.EXTRA_LANGUAGE_PREFERENCE, "tr-TR")
            putExtra(RecognizerIntent.EXTRA_PARTIAL_RESULTS, true)
            putExtra(RecognizerIntent.EXTRA_MAX_RESULTS, 1)
            putExtra(RecognizerIntent.EXTRA_CALLING_PACKAGE, packageName)
        }
        listening = true
        showOverlay("🎙  Dinliyorum…")
        startedAt = SystemClock.elapsedRealtime()
        runCatching { recognizer?.startListening(intent) }.onFailure {
            Log.e(TAG, "dinleme başlamadı", it)
            finish("Mikrofon açılamadı.")
        }
    }

    private fun cancelListening() {
        listening = false
        recognizer?.cancel()
        hideOverlay()
    }

    private var startedAt = 0L

    private val listener = object : RecognitionListener {
        override fun onReadyForSpeech(params: Bundle?) {
            Log.i(TAG, "mikrofon hazır ${SystemClock.elapsedRealtime() - startedAt}ms")
        }
        override fun onBeginningOfSpeech() {}
        override fun onRmsChanged(rmsdB: Float) {}
        override fun onBufferReceived(buffer: ByteArray?) {}
        override fun onEndOfSpeech() { showOverlay("…") }
        override fun onEvent(eventType: Int, params: Bundle?) {}

        override fun onPartialResults(partialResults: Bundle?) {
            val text = partialResults?.getStringArrayList(SpeechRecognizer.RESULTS_RECOGNITION)?.firstOrNull()
            if (!text.isNullOrBlank()) showOverlay("🎙  $text")
        }

        override fun onResults(results: Bundle?) {
            listening = false
            val text = results?.getStringArrayList(SpeechRecognizer.RESULTS_RECOGNITION)?.firstOrNull()
            Log.i(TAG, "duyulan: $text (${SystemClock.elapsedRealtime() - startedAt}ms)")
            if (text.isNullOrBlank()) {
                finish("Seni duyamadım.")
            } else {
                showOverlay("⏳  $text")
                waiting = true
                val gen = ++generation
                io.execute { ask(text, gen) }
            }
        }

        override fun onError(error: Int) {
            listening = false
            Log.w(TAG, "tanıma hatası $error")
            val msg = when (error) {
                SpeechRecognizer.ERROR_NO_MATCH, SpeechRecognizer.ERROR_SPEECH_TIMEOUT -> "Seni duyamadım."
                SpeechRecognizer.ERROR_NETWORK, SpeechRecognizer.ERROR_NETWORK_TIMEOUT -> "Ses tanıma için internet yok."
                SpeechRecognizer.ERROR_INSUFFICIENT_PERMISSIONS -> "Mikrofon izni yok."
                SpeechRecognizer.ERROR_RECOGNIZER_BUSY -> "Ses tanıma meşgul."
                else -> "Ses tanıma hatası ($error)."
            }
            finish(msg)
        }
    }

    private fun ask(text: String, gen: Int) {
        val t0 = SystemClock.elapsedRealtime()
        val reply = runCatching {
            post("/ask", JSONObject().put("text", text))
        }.getOrElse {
            Log.e(TAG, "sunucuya ulaşılamadı", it)
            null
        }
        Log.i(TAG, "sunucu ${SystemClock.elapsedRealtime() - t0}ms: $reply")
        main.post {
            if (gen != generation) return@post
            waiting = false
            if (reply == null) {
                finish("Sunucuya ulaşamadım.")
                return@post
            }
            val actions = reply.optJSONArray("actions") ?: JSONArray()
            for (i in 0 until actions.length()) {
                runCatching { perform(actions.getJSONObject(i)) }.onFailure { Log.e(TAG, "işlem başarısız", it) }
            }
            finish(reply.optString("say", ""))
        }
    }

    private fun post(path: String, body: JSONObject): JSONObject {
        val conn = URL(serverUrl + path).openConnection() as HttpURLConnection
        try {
            conn.requestMethod = "POST"
            conn.connectTimeout = 3000
            conn.readTimeout = 25000
            conn.doOutput = true
            conn.setRequestProperty("Content-Type", "application/json; charset=utf-8")
            conn.setRequestProperty("X-Token", token)
            conn.outputStream.use { it.write(body.toString().toByteArray()) }
            val code = conn.responseCode
            if (code != 200) error("HTTP $code")
            return JSONObject(conn.inputStream.bufferedReader().readText())
        } finally {
            conn.disconnect()
        }
    }

    // Pi'deki Piper sesinden wav alır; ulaşılamazsa null döner ve kutunun kendi sesi kullanılır.
    private fun fetchSpeech(say: String): File? = runCatching {
        val conn = URL("$serverUrl/tts").openConnection() as HttpURLConnection
        try {
            conn.requestMethod = "POST"
            conn.connectTimeout = 2000
            conn.readTimeout = 15000
            conn.doOutput = true
            conn.setRequestProperty("Content-Type", "application/json; charset=utf-8")
            conn.setRequestProperty("X-Token", token)
            conn.outputStream.use { it.write(JSONObject().put("text", say).toString().toByteArray()) }
            if (conn.responseCode != 200) error("HTTP ${conn.responseCode}")
            val f = File(cacheDir, "say.wav")
            conn.inputStream.use { input -> f.outputStream().use { input.copyTo(it) } }
            f
        } finally {
            conn.disconnect()
        }
    }.onFailure { Log.w(TAG, "sunucu sesi alınamadı", it) }.getOrNull()

    private fun speak(say: String) {
        val gen = generation
        io.execute {
            val t0 = SystemClock.elapsedRealtime()
            val wav = fetchSpeech(say)
            Log.i(TAG, "ses ${SystemClock.elapsedRealtime() - t0}ms")
            main.post {
                if (gen != generation) return@post
                if (wav == null || !play(wav)) {
                    if (ttsReady) tts?.speak(say, TextToSpeech.QUEUE_FLUSH, null, "say")
                    main.postDelayed({ if (!listening && !waiting && !isSpeaking()) hideOverlay() }, 3500)
                }
            }
        }
    }

    private fun play(wav: File): Boolean = runCatching {
        val attrs = AudioAttributes.Builder()
            .setUsage(AudioAttributes.USAGE_ASSISTANT)
            .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH)
            .build()
        val audio = getSystemService(AUDIO_SERVICE) as AudioManager
        focus = AudioFocusRequest.Builder(AudioManager.AUDIOFOCUS_GAIN_TRANSIENT_MAY_DUCK)
            .setAudioAttributes(attrs).build().also { audio.requestAudioFocus(it) }
        player = MediaPlayer().apply {
            setAudioAttributes(attrs)
            setDataSource(wav.path)
            setOnCompletionListener { done(it) }
            setOnErrorListener { mp, _, _ -> done(mp); true }
            prepare()
            start()
        }
        true
    }.onFailure {
        Log.e(TAG, "ses çalınamadı", it)
        player?.release()
        player = null
        releaseFocus()
    }.getOrDefault(false)

    private fun done(mp: MediaPlayer) {
        mp.release()
        if (player === mp) player = null
        releaseFocus()
        main.postDelayed({ if (!listening && !waiting && !isSpeaking()) hideOverlay() }, 1200)
    }

    private fun releaseFocus() {
        focus?.let { (getSystemService(AUDIO_SERVICE) as AudioManager).abandonAudioFocusRequest(it) }
        focus = null
    }

    private fun sendApps() {
        val pm = packageManager
        val apps = JSONObject()
        listOf(Intent.CATEGORY_LEANBACK_LAUNCHER, Intent.CATEGORY_LAUNCHER).forEach { cat ->
            val intent = Intent(Intent.ACTION_MAIN).addCategory(cat)
            pm.queryIntentActivities(intent, PackageManager.MATCH_ALL).forEach { ri ->
                val pkg = ri.activityInfo.packageName
                if (pkg == packageName) return@forEach
                val label = ri.loadLabel(pm).toString()
                if (!apps.has(label)) apps.put(label, pkg)
            }
        }
        runCatching { post("/apps", JSONObject().put("apps", apps)) }
            .onSuccess { Log.i(TAG, "uygulama listesi gönderildi: ${apps.length()}") }
            .onFailure { Log.w(TAG, "uygulama listesi gönderilemedi", it) }
    }

    private fun perform(a: JSONObject) {
        val audio = getSystemService(AUDIO_SERVICE) as AudioManager
        when (a.optString("type")) {
            "open_app" -> {
                val pkg = a.optString("package")
                val intent = packageManager.getLeanbackLaunchIntentForPackage(pkg)
                    ?: packageManager.getLaunchIntentForPackage(pkg)
                    ?: return
                startActivity(intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
            }
            "youtube_search" -> {
                val q = a.optString("query")
                val search = Intent(Intent.ACTION_SEARCH)
                    .setPackage(YOUTUBE)
                    .putExtra(SearchManager.QUERY, q)
                    .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
                val ok = runCatching { startActivity(search) }.isSuccess
                if (!ok) {
                    startActivity(
                        Intent(Intent.ACTION_VIEW, Uri.parse("https://www.youtube.com/results?search_query=" + Uri.encode(q)))
                            .setPackage(YOUTUBE)
                            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
                    )
                }
            }
            "volume" -> {
                val stream = AudioManager.STREAM_MUSIC
                when (a.optString("direction")) {
                    "mute" -> audio.adjustStreamVolume(stream, AudioManager.ADJUST_MUTE, AudioManager.FLAG_SHOW_UI)
                    "unmute" -> audio.adjustStreamVolume(stream, AudioManager.ADJUST_UNMUTE, AudioManager.FLAG_SHOW_UI)
                    "up", "down" -> {
                        val dir = if (a.optString("direction") == "up") AudioManager.ADJUST_RAISE else AudioManager.ADJUST_LOWER
                        repeat(a.optInt("steps", 3).coerceIn(1, 15)) {
                            audio.adjustStreamVolume(stream, dir, AudioManager.FLAG_SHOW_UI)
                        }
                    }
                }
            }
            "media" -> {
                val code = when (a.optString("key")) {
                    "play" -> KeyEvent.KEYCODE_MEDIA_PLAY
                    "pause" -> KeyEvent.KEYCODE_MEDIA_PAUSE
                    "play_pause" -> KeyEvent.KEYCODE_MEDIA_PLAY_PAUSE
                    "next" -> KeyEvent.KEYCODE_MEDIA_NEXT
                    "previous" -> KeyEvent.KEYCODE_MEDIA_PREVIOUS
                    "fast_forward" -> KeyEvent.KEYCODE_MEDIA_FAST_FORWARD
                    "rewind" -> KeyEvent.KEYCODE_MEDIA_REWIND
                    "stop" -> KeyEvent.KEYCODE_MEDIA_STOP
                    else -> return
                }
                audio.dispatchMediaKeyEvent(KeyEvent(KeyEvent.ACTION_DOWN, code))
                audio.dispatchMediaKeyEvent(KeyEvent(KeyEvent.ACTION_UP, code))
            }
            "nav" -> when (a.optString("key")) {
                "home" -> performGlobalAction(GLOBAL_ACTION_HOME)
                "back" -> performGlobalAction(GLOBAL_ACTION_BACK)
            }
        }
    }

    private fun finish(say: String) {
        if (say.isBlank()) {
            hideOverlay()
            return
        }
        showOverlay(say)
        speak(say)
    }

    private fun showOverlay(text: String) {
        val wm = getSystemService(WINDOW_SERVICE) as WindowManager
        val view = overlay ?: TextView(this).apply {
            setTextColor(Color.WHITE)
            setTextSize(TypedValue.COMPLEX_UNIT_SP, 20f)
            setPadding(48, 28, 48, 28)
            background = GradientDrawable().apply {
                cornerRadius = 48f
                setColor(Color.argb(225, 32, 32, 36))
            }
            maxWidth = resources.displayMetrics.widthPixels * 2 / 3
        }.also {
            val lp = WindowManager.LayoutParams(
                WindowManager.LayoutParams.WRAP_CONTENT,
                WindowManager.LayoutParams.WRAP_CONTENT,
                WindowManager.LayoutParams.TYPE_ACCESSIBILITY_OVERLAY,
                WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE or WindowManager.LayoutParams.FLAG_NOT_TOUCHABLE,
                PixelFormat.TRANSLUCENT
            ).apply {
                gravity = Gravity.BOTTOM or Gravity.CENTER_HORIZONTAL
                y = 80
            }
            wm.addView(it, lp)
            overlay = it
        }
        view.text = text
    }

    private fun hideOverlay() {
        overlay?.let { runCatching { (getSystemService(WINDOW_SERVICE) as WindowManager).removeView(it) } }
        overlay = null
    }

    override fun onAccessibilityEvent(event: AccessibilityEvent?) {}

    override fun onInterrupt() {
        cancelListening()
    }

    override fun onDestroy() {
        stopSpeaking()
        hideOverlay()
        recognizer?.destroy()
        tts?.shutdown()
        io.shutdown()
        super.onDestroy()
    }

    companion object {
        private const val TAG = "TvClaude"
        private const val YOUTUBE = "com.google.android.youtube.tv"
    }
}
