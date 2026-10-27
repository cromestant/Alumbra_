package ai.alumbra.app

import android.animation.ObjectAnimator
import android.content.Context
import android.text.SpannableString
import android.text.Spanned
import android.text.style.ForegroundColorSpan
import android.util.AttributeSet
import android.view.View
import android.view.ViewGroup
import android.widget.TextView
import androidx.core.content.ContextCompat
import java.time.Instant
import java.time.LocalDate
import java.time.ZoneId
import java.time.format.DateTimeFormatter

/** Small shared UI helpers (mock-look edition). */
object Ui {
    /**
     * Edge-to-edge with manual bar insets: no app content ever touches
     * the status bar or the gesture bar. Call after setContentView.
     */
    fun applyEdgeToEdge(activity: android.app.Activity) {
        androidx.core.view.WindowCompat.setDecorFitsSystemWindows(
            activity.window, false
        )
        androidx.core.view.WindowInsetsControllerCompat(
            activity.window, activity.window.decorView
        ).isAppearanceLightStatusBars = false
        val root = activity.findViewById<android.view.View>(
            android.R.id.content
        )
        androidx.core.view.ViewCompat.setOnApplyWindowInsetsListener(
            root
        ) { view, insets ->
            val bars = insets.getInsets(
                androidx.core.view.WindowInsetsCompat.Type.systemBars()
            )
            view.setPadding(bars.left, bars.top, bars.right, bars.bottom)
            insets
        }
    }

    /** "ALUMBRA." with an amber period, or a plain dim header. */
    fun styleAppName(view: TextView, text: String = "ALUMBRA.") {
        if (!text.endsWith(".")) {
            view.text = text
            return
        }
        val amber = ContextCompat.getColor(view.context, R.color.alumbra_amber)
        val out = SpannableString(text)
        out.setSpan(
            ForegroundColorSpan(amber), text.length - 1, text.length,
            Spanned.SPAN_EXCLUSIVE_EXCLUSIVE,
        )
        view.text = out
    }

    /** Gentle looping alpha pulse; cancel when hidden. */
    fun pulse(view: View): ObjectAnimator {
        return ObjectAnimator.ofFloat(view, "alpha", 1f, 0.35f).apply {
            duration = 900
            repeatMode = ObjectAnimator.REVERSE
            repeatCount = ObjectAnimator.INFINITE
            start()
        }
    }

    /** "Today 2:14 PM" / "Yesterday 9:02 AM" / "Oct 24 4:40 PM". */
    fun relativeDay(context: Context, epochMs: Long): String {
        val zone = ZoneId.systemDefault()
        val day = Instant.ofEpochMilli(epochMs).atZone(zone).toLocalDate()
        val today = LocalDate.now(zone)
        val time = Instant.ofEpochMilli(epochMs).atZone(zone)
            .format(DateTimeFormatter.ofPattern("h:mm a"))
        return when (day) {
            today -> "Today $time"
            today.minusDays(1) -> "Yesterday $time"
            else -> Instant.ofEpochMilli(epochMs).atZone(zone)
                .format(DateTimeFormatter.ofPattern("MMM d h:mm a"))
        }
    }

    fun dimColor(context: Context): Int =
        ContextCompat.getColor(context, R.color.alumbra_dim)

    fun amberColor(context: Context): Int =
        ContextCompat.getColor(context, R.color.alumbra_amber)

    fun warnColor(context: Context): Int =
        ContextCompat.getColor(context, R.color.alumbra_warn)

    fun helpColor(context: Context): Int =
        ContextCompat.getColor(context, R.color.alumbra_help)
}

/** Minimal flow layout for the What-I-saw chips. No dependency. */
class FlowLayout @JvmOverloads constructor(
    context: Context,
    attrs: AttributeSet? = null,
) : ViewGroup(context, attrs) {
    var gapPx: Int = (8 * resources.displayMetrics.density).toInt()

    override fun onMeasure(widthSpec: Int, heightSpec: Int) {
        val maxWidth = MeasureSpec.getSize(widthSpec) - paddingLeft - paddingRight
        var x = 0
        var y = 0
        var rowHeight = 0
        for (i in 0 until childCount) {
            val child = getChildAt(i)
            if (child.visibility == GONE) continue
            measureChild(child, widthSpec, heightSpec)
            if (x > 0 && x + child.measuredWidth > maxWidth) {
                x = 0
                y += rowHeight + gapPx
                rowHeight = 0
            }
            x += child.measuredWidth + gapPx
            rowHeight = maxOf(rowHeight, child.measuredHeight)
        }
        setMeasuredDimension(
            MeasureSpec.getSize(widthSpec),
            y + rowHeight + paddingTop + paddingBottom,
        )
    }

    override fun onLayout(changed: Boolean, l: Int, t: Int, r: Int, b: Int) {
        val maxWidth = r - l - paddingLeft - paddingRight
        var x = paddingLeft
        var y = paddingTop
        var rowHeight = 0
        for (i in 0 until childCount) {
            val child = getChildAt(i)
            if (child.visibility == GONE) continue
            if (x > paddingLeft && x + child.measuredWidth > maxWidth + paddingLeft) {
                x = paddingLeft
                y += rowHeight + gapPx
                rowHeight = 0
            }
            child.layout(x, y, x + child.measuredWidth, y + child.measuredHeight)
            x += child.measuredWidth + gapPx
            rowHeight = maxOf(rowHeight, child.measuredHeight)
        }
    }
}
