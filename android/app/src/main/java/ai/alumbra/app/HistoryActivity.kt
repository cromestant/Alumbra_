package ai.alumbra.app

import android.content.Intent
import android.os.Bundle
import android.view.View
import android.widget.ArrayAdapter
import android.widget.ListView
import android.widget.TextView
import androidx.appcompat.app.AppCompatActivity
import java.time.Instant
import java.time.ZoneId
import java.time.format.DateTimeFormatter

/** History: every ask, newest first. Tap opens it in the Answer screen. */
class HistoryActivity : AppCompatActivity() {

    override fun onCreate(savedInstanceState: Bundle?) {
        Prefs.applyTheme(this)
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_history)

        val listView: ListView = findViewById(R.id.history_list)
        val emptyView: TextView = findViewById(R.id.history_empty)
        val records = HistoryStore.list(this)
        if (records.isEmpty()) {
            emptyView.visibility = View.VISIBLE
            listView.visibility = View.GONE
            return
        }
        val formatter = DateTimeFormatter.ofPattern("MMM d HH:mm")
        val labels = records.map { r ->
            val whenText = Instant.ofEpochMilli(r.timestamp)
                .atZone(ZoneId.systemDefault()).format(formatter)
            val head = r.question?.take(60)
                ?: r.answer?.take(60)
                ?: r.error?.take(60)
                ?: "(empty ask)"
            val state = r.error?.let { " — error" }
                ?: r.behavior?.let { " — $it" }.orEmpty()
            "$whenText — $head$state"
        }
        listView.adapter = ArrayAdapter(
            this, android.R.layout.simple_list_item_1, labels
        )
        listView.setOnItemClickListener { _, _, position, _ ->
            startActivity(
                Intent(this, AnswerActivity::class.java).putExtra(
                    AnswerActivity.EXTRA_RECORD_ID, records[position].id
                )
            )
        }
    }
}
