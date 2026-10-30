package ai.alumbra.app

import android.content.Context
import android.content.Intent
import android.os.Bundle
import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import android.widget.ArrayAdapter
import android.widget.ListView
import android.widget.TextView
import androidx.appcompat.app.AppCompatActivity

/** History: every ask, newest first. Tap opens it in the Answer screen. */
class HistoryActivity : AppCompatActivity() {

    override fun onCreate(savedInstanceState: Bundle?) {
        Prefs.applyTheme(this)
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_history)
        Ui.applyEdgeToEdge(this)
        Ui.styleAppName(findViewById(R.id.appname_text), "HISTORY")

        val listView: ListView = findViewById(R.id.history_list)
        val emptyView: TextView = findViewById(R.id.history_empty)
        val records = HistoryStore.list(this)
        if (records.isEmpty()) {
            emptyView.visibility = View.VISIBLE
            listView.visibility = View.GONE
            return
        }
        listView.adapter = HistoryAdapter(this, records)
        listView.setOnItemClickListener { _, _, position, _ ->
            startActivity(
                Intent(this, AnswerActivity::class.java).putExtra(
                    AnswerActivity.EXTRA_RECORD_ID, records[position].id
                ).putExtra(
                    AnswerActivity.EXTRA_FROM_HISTORY, true
                )
            )
        }
    }

    private class HistoryAdapter(
        context: Context,
        private val records: List<HistoryRecord>,
    ) : ArrayAdapter<HistoryRecord>(context, 0, records) {
        override fun getView(
            position: Int, convertView: View?, parent: ViewGroup
        ): View {
            val view = convertView ?: LayoutInflater.from(context)
                .inflate(R.layout.history_row, parent, false)
            val record = records[position]
            view.findViewById<TextView>(R.id.row_q).text =
                record.question ?: "(no question)"
            view.findViewById<TextView>(R.id.row_a).text =
                record.error ?: record.answer?.take(120)
                ?: "(no answer)"
            view.findViewById<TextView>(R.id.row_t).text =
                Ui.relativeDay(context, record.timestamp)
            view.findViewById<TextView>(R.id.row_tag_esc).visibility =
                if (record.error == null && record.behavior == "ESCALATE") {
                    View.VISIBLE
                } else {
                    View.GONE
                }
            view.findViewById<TextView>(R.id.row_tag_human).visibility =
                if (record.behavior == HistoryStore.BEHAVIOR_VOLUNTEER) {
                    View.VISIBLE
                } else {
                    View.GONE
                }
            view.findViewById<TextView>(R.id.row_tag_un).visibility =
                if (record.error != null) View.VISIBLE else View.GONE
            return view
        }
    }
}
