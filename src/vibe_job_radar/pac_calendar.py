"""Pure local/GMT calendar helpers for explicitly trusted full-URL PAC scripts."""

# Independent implementation of the documented subset in FULL_URL_PAC.md.
# No host callbacks; cyclic intervals wrap, absolute years must be ordered.
CALENDAR_HELPERS = r"""
(function () {
    const weekdays = ['SUN', 'MON', 'TUE', 'WED', 'THU', 'FRI', 'SAT'];
    const months = ['JAN', 'FEB', 'MAR', 'APR', 'MAY', 'JUN',
                    'JUL', 'AUG', 'SEP', 'OCT', 'NOV', 'DEC'];
    function invalid() { throw new Error('PAC calendar arguments'); }
    function integer(value, low, high) {
        if (!Number.isInteger(value) || value < low || value > high) invalid();
        return value;
    }
    function named(value, names) {
        if (typeof value !== 'string') invalid();
        const index = names.indexOf(value);
        if (index < 0) invalid();
        return index;
    }
    function unpack(args, sizes) {
        const values = Array.from(args);
        const utc = values[values.length - 1] === 'GMT';
        if (utc) values.pop();
        if (sizes.indexOf(values.length) < 0) invalid();
        const date = new Date();
        if (!Number.isFinite(date.getTime())) invalid();
        function field(name) { return date['get' + (utc ? 'UTC' : '') + name](); }
        return {values: values, year: field('FullYear'), month: field('Month'),
                day: field('Date'), weekday: field('Day'), hour: field('Hours'),
                minute: field('Minutes'), second: field('Seconds')};
    }
    function between(now, start, end, cyclic) {
        if (start <= end) return start <= now && now <= end;
        if (!cyclic) invalid();
        return now >= start || now <= end;
    }
    function kind(value) {
        if (typeof value === 'string') { named(value, months); return 'month'; }
        integer(value, 1, 9999);
        return value <= 31 ? 'day' : 'year';
    }
    function year(value) { return integer(value, 32, 9999); }
    function calendarDay(day, month, fullYear) {
        integer(day, 1, 31);
        const leap = fullYear % 4 === 0 &&
                     (fullYear % 100 !== 0 || fullYear % 400 === 0);
        const lengths = [31, leap ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];
        if (day > lengths[month]) invalid();
        return day;
    }
    globalThis.weekdayRange = function () {
        const clock = unpack(arguments, [1, 2]), v = clock.values;
        const start = named(v[0], weekdays);
        const end = v.length === 1 ? start : named(v[1], weekdays);
        return between(clock.weekday, start, end, true);
    };
    globalThis.timeRange = function () {
        const clock = unpack(arguments, [1, 2, 4, 6]), v = clock.values;
        if (v.length <= 2) {
            const start = integer(v[0], 0, 23);
            return between(clock.hour, start,
                           v.length === 1 ? start : integer(v[1], 0, 23), true);
        }
        const width = v.length / 2;
        function point(offset) {
            const minutes = integer(v[offset], 0, 23) * 60 +
                            integer(v[offset + 1], 0, 59);
            return width === 2 ? minutes : minutes * 60 + integer(v[offset + 2], 0, 59);
        }
        const minutes = clock.hour * 60 + clock.minute;
        const now = width === 2 ? minutes : minutes * 60 + clock.second;
        return between(now, point(0), point(width), true);
    };
    globalThis.dateRange = function () {
        const clock = unpack(arguments, [1, 2, 4, 6]), v = clock.values;
        if (v.length <= 2) {
            const type = kind(v[0]);
            if (v.length === 2 && kind(v[1]) !== type) invalid();
            const start = type === 'month' ? named(v[0], months) : v[0];
            const end = v.length === 1 ? start :
                        type === 'month' ? named(v[1], months) : v[1];
            return between(clock[type], start, end, type !== 'year');
        }
        if (v.length === 4 && typeof v[0] === 'string') {
            const start = year(v[1]) * 12 + named(v[0], months);
            const end = year(v[3]) * 12 + named(v[2], months);
            return between(clock.year * 12 + clock.month, start, end, false);
        }
        const width = v.length / 2, absolute = width === 3;
        function point(offset) {
            const month = named(v[offset + 1], months);
            // February 29 is valid in a recurring annual pattern, matching
            // only actual leap days. Other impossible month/day pairs reject.
            const fullYear = absolute ? year(v[offset + 2]) : 2000;
            const day = calendarDay(v[offset], month, fullYear);
            return (absolute ? fullYear * 12 + month : month) * 32 + day;
        }
        const now = (absolute ? clock.year * 12 + clock.month : clock.month) * 32 + clock.day;
        return between(now, point(0), point(width), !absolute);
    };
})();
"""
