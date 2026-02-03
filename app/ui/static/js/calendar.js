// FloTrader Calendar UI JavaScript

document.addEventListener('DOMContentLoaded', function() {
    // Add keyboard navigation
    document.addEventListener('keydown', function(e) {
        // Left arrow - previous month
        if (e.key === 'ArrowLeft' && e.altKey) {
            const prevBtn = document.querySelector('.nav-btn:first-of-type');
            if (prevBtn) {
                window.location = prevBtn.href;
            }
        }
        // Right arrow - next month
        if (e.key === 'ArrowRight' && e.altKey) {
            const nextBtn = document.querySelector('.nav-btn:last-of-type');
            if (nextBtn) {
                window.location = nextBtn.href;
            }
        }
    });

    // Add tooltips to days with trades
    const days = document.querySelectorAll('.day.profitable, .day.losing');
    days.forEach(day => {
        const tradeCount = day.querySelector('.trade-count');
        const pnl = day.querySelector('.pnl');

        if (tradeCount && pnl) {
            day.title = `${tradeCount.textContent.trim()} | ${pnl.textContent.trim()}`;
        }
    });

    // Highlight current week
    const today = document.querySelector('.day.today');
    if (today) {
        // Find the week row containing today
        const allDays = Array.from(document.querySelectorAll('.calendar-grid > .day, .calendar-grid > .empty'));
        const todayIndex = allDays.indexOf(today);

        if (todayIndex !== -1) {
            const weekStart = Math.floor(todayIndex / 7) * 7;
            for (let i = weekStart; i < weekStart + 7 && i < allDays.length; i++) {
                if (!allDays[i].classList.contains('empty')) {
                    allDays[i].style.opacity = '1';
                }
            }
        }
    }

    console.log('FloTrader Calendar UI loaded');
});
