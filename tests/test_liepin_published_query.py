"""Invented values exercise transformations in saved publisher search code."""
import copy
import hashlib
import json
import unittest
from urllib.parse import urlencode

from vibe_job_radar.guided.adapters import builtins
from vibe_job_radar.guided.contracts import CrawlError, PageSnapshot
from vibe_job_radar.guided.liepin_search import request_context
from vibe_job_radar.guided.native_browser import BusinessObservation
from vibe_job_radar.guided.search_scope import check_scope


ADAPTER = builtins().get('liepin')
OLD_ID, NEW_ID = 'a' * 32, 'b' * 32


def published_request(**updates):
    main = dict(city='410', dq='410', pubTime='', currentPage=0, pageSize=40,
                key='架构师 C++ / AI', suggestTag='', workYearCode='', compId='',
                compName='', compTag='', industry='', salaryCode='', jobKind='',
                compScale='', compKind='', compStage='', eduLevel='')
    main.update(updates)
    through = dict(scene='fixture-search', skId='', fkId='', ckId=OLD_ID, suggest=None)
    query = {**main, **through, 'suggest':'null', 'suggestId':''}
    form = dict(main)
    form['hrActiveTimeCode'] = form.pop('pubTime') or ''
    bounds = form['salaryCode'].split('$') if '$' in form['salaryCode'] else []
    form.update(salaryCode='' if bounds else form['salaryCode'],
                salaryLow=bounds[0] if bounds else '', salaryHigh=bounds[1] if bounds else '')
    body = dict(data=dict(mainSearchPcConditionForm=form,
                          passThroughForm={**through, 'ckId': NEW_ID}))
    return query, body


def bind(query, body):
    url = ADAPTER.search_base + '?' + urlencode(query)
    context = request_context(ADAPTER, 'liepin_search', {'postData':json.dumps(body)}, url)
    return context, ADAPTER.accept_url(url)


class PublishedQueryBindingTests(unittest.TestCase):
    def test_full_visible_search_shape_binds_without_retaining_values(self):
        query, body = published_request()
        context, url = bind(query, body)
        self.assertEqual(context, {'query':hashlib.sha256(url.encode()).hexdigest(), 'page':0, 'size':40})
        self.assertNotIn('架构师', json.dumps(context, ensure_ascii=False))
        self.assertNotIn(OLD_ID, repr(context))
        self.assertNotIn(NEW_ID, repr(context))

    def test_date_alias_and_both_salary_shapes_bind(self):
        for fields in (dict(pubTime='7'), dict(salaryCode='10$30'), dict(salaryCode='fixture-band'),
                       dict(salaryCode='$30'), dict(salaryCode='10$'), dict(suggestTag='fixture-tag')):
            with self.subTest(fields=fields):
                query, body = published_request(**fields)
                self.assertEqual(bind(query, body)[0]['page'], 0)

    def test_suggestion_object_is_bound_by_its_identifier_not_object_string(self):
        query, body = published_request()
        query.update(suggest='[object Object]', suggestId='fixture-suggestion')
        body['data']['passThroughForm']['suggest'] = {'suggestId':'fixture-suggestion'}
        self.assertTrue(bind(query, body)[0])
        for change in ({'suggestId':'other'}, {'suggestId':'fixture-suggestion', 'unknown':'hidden'}):
            bad = copy.deepcopy(body)
            bad['data']['passThroughForm']['suggest'] = change
            with self.subTest(change=change), self.assertRaisesRegex(CrawlError, 'liepin_search_query_mismatch'):
                bind(query, bad)

    def test_keyword_filters_alias_bounds_and_tracking_mismatches_are_rejected(self):
        query, body = published_request(pubTime='7', salaryCode='10$30')
        changes = [('mainSearchPcConditionForm', k, v) for k, v in
                   (('key','other'), ('city','020'), ('dq','020'), ('hrActiveTimeCode','30'),
                    ('pubTime','30'), ('salaryLow','11'), ('salaryHigh','31'), ('salaryCode','10$30'),
                    ('currentPage',1), ('pageSize',20), ('suggestTag','added'))]
        changes += [('passThroughForm', k, v) for k, v in
                    (('scene','different'), ('skId','different'), ('fkId','different'),
                     ('ckId','invalid'), ('suggest',{'suggestId':'added'}))]
        for section, field, value in changes:
            bad = copy.deepcopy(body)
            bad['data'][section][field] = value
            with self.subTest(section=section, field=field), self.assertRaisesRegex(CrawlError, 'liepin_search_query_mismatch'):
                bind(query, bad)

    def test_new_ckid_is_not_permission_to_ignore_other_request_fields(self):
        query, body = published_request()
        for value in ('', 'C'*32, 'c'*31, 'c'*33, 'c'*31+'-', None, 1, True):
            bad = copy.deepcopy(body)
            bad['data']['passThroughForm']['ckId'] = value
            with self.subTest(value=value), self.assertRaisesRegex(CrawlError, 'liepin_search_query_mismatch'):
                bind(query, bad)
        for field in ('city', 'pubTime', 'salaryCode', 'scene', 'skId', 'fkId', 'ckId'):
            bad = dict(query)
            bad.pop(field)
            # Empty omitted filters can remain empty; a nonempty condition or
            # pass-through field cannot be silently removed from its URL.
            if query[field] == '': continue
            with self.subTest(removed=field), self.assertRaisesRegex(CrawlError, 'liepin_search_query_mismatch'):
                bind(bad, body)

    def test_unknown_or_duplicate_url_fields_and_unmapped_payload_fields_stop(self):
        query, body = published_request()
        for field in ('unknownFilter', 'authorization'):
            with self.subTest(field=field), self.assertRaises(CrawlError):
                bind({**query, field:'fixture'}, body)
        url = ADAPTER.search_base + '?' + urlencode(query) + '&city=410'
        with self.assertRaisesRegex(CrawlError, 'liepin_search_query_mismatch'):
            request_context(ADAPTER, 'liepin_search', {'postData':json.dumps(body)}, url)
        for section in ('mainSearchPcConditionForm', 'passThroughForm'):
            bad = copy.deepcopy(body)
            bad['data'][section]['unknownFilter'] = 'fixture'
            with self.subTest(section=section), self.assertRaisesRegex(CrawlError, 'liepin_search_query_mismatch'):
                bind(query, bad)

    def test_null_suggestion_cannot_hide_a_nonempty_suggestion_id(self):
        query, body = published_request()
        with self.assertRaisesRegex(CrawlError, 'liepin_search_query_mismatch'):
            bind({**query, 'suggestId':'unexpected'}, body)
        for value in ([], {}, 'null', False):
            bad = copy.deepcopy(body)
            bad['data']['passThroughForm']['suggest'] = value
            with self.subTest(value=value), self.assertRaisesRegex(CrawlError, 'liepin_search_query_mismatch'):
                bind(query, bad)

    def test_metadata_rotation_does_not_reuse_a_response_for_another_url(self):
        query, body = published_request()
        context, _ = bind(query, body)
        other, url = bind({**query, 'ckId':'c'*32}, body)
        self.assertNotEqual(context['query'], other['query'])
        page = PageSnapshot(url, '<a href="https://www.liepin.com/job/123.shtml">旧岗位</a>',
            (BusinessObservation(1, 'liepin_search', 1, {}, context),), business_required=True)
        with self.assertRaisesRegex(CrawlError, 'page_not_ready'):
            ADAPTER.cards(page)

    def test_pagination_keeps_conditions_while_publisher_interaction_fields_change(self):
        query, body = published_request()
        _, first = bind(query, body)
        state = dict(query_scope_version=1, search_url=ADAPTER.search_url(query['key']), keyword=query['key'])
        self.assertEqual(check_scope(state, ADAPTER, first), '0')
        updated = {**query, 'scene':'fixture-next-page', 'ckId':'c'*32,
                   'skId':'d'*32, 'fkId':'e'*32, 'currentPage':1}
        second = ADAPTER.search_base + '?' + urlencode(updated)
        self.assertEqual(check_scope(state, ADAPTER, second), '1')
        for name in ('scene', 'skId', 'fkId', 'ckId'):
            self.assertNotIn(name, state['effective_search'])
        self.assertEqual(state['effective_search']['city'], '410')
        for fields in ({'city':'020'}, {'salaryCode':'10$30'}, {'pubTime':'7'},
                       {'suggestId':'other'}, {'suggestTag':'other'}, {'unknownFilter':'other'}):
            with self.subTest(fields=fields), self.assertRaisesRegex(CrawlError, 'search_scope_changed'):
                check_scope(state, ADAPTER, ADAPTER.search_base+'?'+urlencode({**updated, **fields}))

    def test_explicit_filter_cannot_be_replaced_by_publisher_defaults(self):
        query, body = published_request()
        _, actual = bind(query, body)
        for name, value in (('city','010'), ('salaryCode','10$30'), ('suggestTag','wanted')):
            state = dict(query_scope_version=1, keyword=query['key'],
                search_url=ADAPTER.search_url(query['key'])+'&'+urlencode({name:value}))
            with self.subTest(name=name), self.assertRaisesRegex(CrawlError, 'search_scope_changed'):
                check_scope(state, ADAPTER, actual)
            self.assertNotIn('effective_search', state)


if __name__ == '__main__':
    unittest.main()
