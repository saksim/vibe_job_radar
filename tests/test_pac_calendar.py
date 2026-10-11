"""Fixed clock checks of inclusive PAC calendar rules and explicit rejections."""
from datetime import datetime, timezone
import json
import unittest
from vibe_job_radar import pac_url_worker
from vibe_job_radar.pac_calendar import CALENDAR_HELPERS


class PacCalendarTests(unittest.TestCase):
    def context(self, local='2026-10-09T13:59:59.999', utc=None):
        import quickjs
        local = datetime.fromisoformat(local)
        utc = datetime.fromisoformat(utc) if utc else local
        values = {}
        for prefix, date in (('get', local), ('getUTC', utc)):
            for name, value in {
                    'FullYear': date.year, 'Month': date.month-1, 'Date': date.day,
                    'Day': (date.weekday()+1) % 7, 'Hours': date.hour,
                    'Minutes': date.minute, 'Seconds': date.second}.items():
                values[prefix+name] = value
        values['getTime'] = int(utc.replace(tzinfo=timezone.utc).timestamp()*1000)
        methods = ','.join(json.dumps(k)+':function(){return '+str(v)+';}'
                           for k, v in values.items())
        context = quickjs.Context()
        context.set_memory_limit(pac_url_worker.MEMORY_LIMIT)
        context.set_max_stack_size(pac_url_worker.STACK_LIMIT)
        context.set_time_limit(pac_url_worker.CPU_LIMIT)
        context.eval('var clockReads=0; Date=function(){clockReads++; return {'+methods+'};};')
        context.eval(CALENDAR_HELPERS)
        return context

    def check(self, helper, args, expected, **clock):
        context = self.context(**clock)
        self.assertIs(context.get(helper)(*args), expected, (helper, args, clock))
        self.assertEqual(context.eval('clockReads'), 1)

    def test_weekday_single_inclusive_and_wrapped_interval(self):
        if not pac_url_worker.available(): return
        for args, expected in [
                (['FRI'], True), (['THU'], False), (['MON','FRI'], True),
                (['SAT','THU'], False), (['FRI','MON'], True)]:
            self.check('weekdayRange', args, expected)
        self.check('weekdayRange', ['FRI','MON'], True, local='2026-10-11T12:00:00')
        self.check('weekdayRange', ['FRI','MON'], False, local='2026-10-13T12:00:00')

    def test_date_all_overloads_and_inclusive_complete_end_date(self):
        if not pac_url_worker.available(): return
        for args, expected in [
                ([9], True), ([10], False), (['OCT'], True), (['NOV'], False),
                ([2026], True), ([2025], False), ([8,9], True), ([10,20], False),
                (['SEP','OCT'], True), (['NOV','DEC'], False),
                ([2025,2026], True), ([2027,2028], False),
                ([1,'SEP',9,'OCT'], True), ([10,'OCT',15,'NOV'], False),
                (['OCT',2025,'OCT',2026], True), (['NOV',2026,'DEC',2026], False),
                ([9,'OCT',2026,9,'OCT',2026], True),
                ([10,'OCT',2026,1,'JAN',2027], False)]:
            self.check('dateRange', args, expected, local='2026-10-09T23:59:59.999')

    def test_recurring_dates_wrap_and_leap_days_use_actual_calendar(self):
        if not pac_url_worker.available(): return
        cases = [
            ([30,2], '2026-12-31T12:00:00', True),
            ([30,2], '2026-12-15T12:00:00', False),
            (['DEC','JAN'], '2027-01-01T12:00:00', True),
            (['DEC','JAN'], '2026-11-30T12:00:00', False),
            ([20,'DEC',5,'JAN'], '2027-01-01T12:00:00', True),
            ([20,'DEC',5,'JAN'], '2026-06-01T12:00:00', False),
            ([29,'FEB',29,'FEB'], '2024-02-29T12:00:00', True),
            ([29,'FEB',29,'FEB'], '2023-02-28T12:00:00', False),
            ([29,'FEB',2000,29,'FEB',2000], '2000-02-29T23:59:59.999', True),
            (['DEC',2026,'JAN',2027], '2027-01-31T23:59:59.999', True),
            (['DEC',2026,'JAN',2027], '2027-02-01T00:00:00', False)]
        for args, local, expected in cases:
            with self.subTest(args=args, local=local):
                self.check('dateRange', args, expected, local=local)

    def test_time_precision_includes_entire_last_unit_and_wraps_midnight(self):
        if not pac_url_worker.available(): return
        cases = [
            ([13], '2026-10-09T13:59:59.999', True),
            ([13], '2026-10-09T14:00:00', False),
            ([12,13], '2026-10-09T13:59:59.999', True),
            ([12,13], '2026-10-09T14:00:00', False),
            ([8,30,17,0], '2026-10-09T08:30:00', True),
            ([8,30,17,0], '2026-10-09T17:00:59.999', True),
            ([8,30,17,0], '2026-10-09T17:01:00', False),
            ([0,0,0,0,0,30], '2026-10-09T00:00:30.999', True),
            ([0,0,0,0,0,30], '2026-10-09T00:00:31', False),
            ([23,1], '2026-10-09T00:20:00', True),
            ([23,1], '2026-10-09T02:00:00', False),
            ([23,50,0,10], '2026-10-09T00:10:59.999', True),
            ([23,50,0,10], '2026-10-09T00:11:00', False),
            ([23,59,50,0,0,10], '2026-10-09T00:00:05', True),
            ([23,59,50,0,0,10], '2026-10-09T23:00:00', False)]
        for args, local, expected in cases:
            with self.subTest(args=args, local=local):
                self.check('timeRange', args, expected, local=local)

    def test_gmt_uses_utc_across_local_day_month_and_year_boundary(self):
        if not pac_url_worker.available(): return
        clock = dict(local='2026-12-31T18:02:03', utc='2027-01-01T01:02:03')
        for helper, args, local_result, utc_result in [
                ('weekdayRange',['THU'],True,False),
                ('weekdayRange',['FRI'],False,True),
                ('dateRange',[31],True,False), ('dateRange',[1],False,True),
                ('dateRange',['DEC'],True,False), ('dateRange',['JAN'],False,True),
                ('dateRange',[2026],True,False), ('dateRange',[2027],False,True),
                ('dateRange',[1,'JAN',2027,31,'JAN',2027],False,True),
                ('timeRange',[18],True,False), ('timeRange',[1],False,True),
                ('timeRange',[1,2,1,2],False,True),
                ('timeRange',[1,2,3,1,2,3],False,True)]:
            self.check(helper,args,local_result,**clock)
            self.check(helper,args+['GMT'],utc_result,**clock)

    def test_malformed_or_ambiguous_arguments_reject_without_guessing(self):
        if not pac_url_worker.available(): return
        import quickjs
        bad = {
            'weekdayRange': [[], ['GMT'], ['FRI','SAT','SUN'], ['fri'],
                             ['__proto__'], [True], ['FRI','GMT','SAT']],
            'dateRange': [[], [0], [-1], [10000], [True], [1.5], [None],
                          [float('nan')], [float('inf')], ['2026'], ['oct'],
                          [9,'OCT'], ['OCT',2026], [1,2,3], [2027,2026],
                          [31,'APR',31,'APR'], [29,'FEB',1900,1,'MAR',1900],
                          [29,'FEB',2023,1,'MAR',2023],
                          ['OCT',2027,'JAN',2026],
                          [1,'JAN',2027,31,'DEC',2026]],
            'timeRange': [[], [24], [-1], [True], ['13'], [13.5], [1,2,3],
                          [1,60,2,0], [1,0,60,2,0,0], [1,0,0,2,0,-1],
                          [1,2,3,4,5], [0,'GMT',23]]}
        for helper, groups in bad.items():
            for args in groups:
                with self.subTest(helper=helper,args=args):
                    with self.assertRaisesRegex(quickjs.JSException,'PAC calendar arguments'):
                        self.context().get(helper)(*args)

    def test_helpers_do_not_add_host_io_or_modify_input_arrays(self):
        if not pac_url_worker.available(): return
        context = self.context()
        self.assertEqual(context.eval(
            "['dnsResolve','myIpAddress','fetch','std','os','Python'].filter("
            "function(name){return typeof globalThis[name]!=='undefined';}).length"), 0)
        self.assertEqual(context.eval(
            "var args=[12,13,'GMT']; var before=JSON.stringify(args);"
            "timeRange.apply(null,args); JSON.stringify(args)===before;"), True)
        self.assertEqual(context.eval('clockReads'), 1)
