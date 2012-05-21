import json
import tempfile

from django.conf import settings

from mock import patch
from nose.tools import eq_

from addons.models import Addon, Category
import amo
from amo.tests import AMOPaths
from files.models import FileUpload
from mkt.api.tests.test_oauth import BaseOAuth
from mkt.webapps.models import Webapp
from users.models import UserProfile


class ValidationHandler(BaseOAuth):
    fixtures = ['base/user_2519', 'base/users']

    def setUp(self):
        super(ValidationHandler, self).setUp()
        self.list_url = ('api_dispatch_list', {'resource_name': 'validation'})
        self.get_url = None
        self.consumer = self.accepted_consumer
        self.user = UserProfile.objects.get(pk=2519)

    def create(self):
        res = self.client.post(self.list_url,
                               body=json.dumps({'manifest':
                                                'http://foo.com'}))
        self.get_url = ('api_dispatch_detail',
                        {'resource_name': 'validation',
                         'pk': json.loads(res.content)['id']})
        return res

    def get(self):
        return FileUpload.objects.all()[0]

    def get_error(self, response):
        return json.loads(response.content)['error_message']


@patch.object(settings, 'SITE_URL', 'http://api/')
class TestAddValidationHandler(ValidationHandler):

    def test_verbs(self):
        self._allowed_verbs(self.list_url, ['post'])

    def test_good(self):
        res = self.create()
        eq_(res.status_code, 201)  # Note! This should be a 202.
        content = json.loads(res.content)
        eq_(FileUpload.objects.filter(uuid=content['id']).count(), 1)

    @patch('mkt.api.resources.tasks.fetch_manifest.delay')
    def test_fetch(self, fetch):
        self.create()
        assert fetch.called

    def test_missing(self):
        res = self.client.post(self.list_url, body=json.dumps({}))
        eq_(res.status_code, 400)
        eq_(self.get_error(res)['manifest'], ['This field is required.'])

    def test_bad(self):
        res = self.client.post(self.list_url,
                               body=json.dumps({'manifest': 'blurgh'}))
        eq_(res.status_code, 400)
        eq_(self.get_error(res)['manifest'], ['Enter a valid URL.'])


@patch.object(settings, 'SITE_URL', 'http://api/')
class TestGetValidationHandler(ValidationHandler):

    def create(self):
        res = FileUpload.objects.create(user=self.user, path='http://foo.com')
        self.get_url = ('api_dispatch_detail',
                        {'resource_name': 'validation', 'pk': res.pk})
        return res

    def test_verbs(self):
        self.create()
        self._allowed_verbs(self.get_url, ['get'])

    def test_check(self):
        self.create()
        res = self.client.get(self.get_url)
        eq_(res.status_code, 200)

    def test_not_owner(self):
        obj = self.create()
        obj.update(user=UserProfile.objects.get(email='admin@mozilla.com'))
        res = self.client.get(self.get_url)
        eq_(res.status_code, 401)

    def test_not_found(self):
        url = ('api_dispatch_detail',
                {'resource_name': 'validation', 'pk': '123123123'})
        res = self.client.get(url)
        eq_(res.status_code, 404)

    def test_not_run(self):
        self.create()
        res = self.client.get(self.get_url)
        eq_(res.status_code, 200)
        eq_(json.loads(res.content)['processed'], False)

    def test_pass(self):
        obj = self.create()
        obj.update(valid=True)
        res = self.client.get(self.get_url)
        eq_(res.status_code, 200)
        data = json.loads(res.content)
        eq_(data['processed'], True)
        eq_(data['valid'], True)

    def test_failure(self):
        obj = self.create()
        error = '{"errors": 1, "messages": [{"tier": 1, "message": "nope"}]}'
        obj.update(valid=False, validation=error)
        res = self.client.get(self.get_url)
        eq_(res.status_code, 200)
        data = json.loads(res.content)
        eq_(data['processed'], True)
        eq_(data['valid'], False)
        eq_(data['validation'], json.loads(error))


class CreateHandler(BaseOAuth):
    fixtures = ['base/user_2519', 'base/users', 'base/platforms']

    def setUp(self):
        super(CreateHandler, self).setUp()
        self.list_url = ('api_dispatch_list', {'resource_name': 'app'})
        self.user = UserProfile.objects.get(pk=2519)
        self.file = tempfile.NamedTemporaryFile('w', suffix='.webapp').name
        self.manifest_copy_over(self.file, 'mozball-nice-slug.webapp')

    def create(self):
        return FileUpload.objects.create(user=self.user, path=self.file,
                                         name=self.file, valid=True)

    def get_error(self, response):
        return json.loads(response.content)['error_message']


@patch.object(settings, 'SITE_URL', 'http://api/')
class TestAppCreateHandler(CreateHandler, AMOPaths):

    def count(self):
        return Addon.objects.count()

    def test_verbs(self):
        obj = self.create()
        self._allowed_verbs(self.list_url, ['post'])
        obj = self.create_app()
        self._allowed_verbs(self.get_url, ['get'])

    def test_not_valid(self):
        obj = self.create()
        obj.update(valid=False)
        res = self.client.post(self.list_url,
                               body=json.dumps({'manifest': obj.uuid}))
        eq_(res.status_code, 400)
        eq_(self.get_error(res)['manifest'], ['Upload not valid.'])
        eq_(self.count(), 0)

    def test_not_there(self):
        res = self.client.post(self.list_url,
                               body=json.dumps({'manifest':
                                   'some-random-32-character-stringy'}))
        eq_(res.status_code, 400)
        eq_(self.get_error(res)['manifest'], ['No upload found.'])
        eq_(self.count(), 0)

    def test_not_yours(self):
        obj = self.create()
        obj.update(user=UserProfile.objects.get(email='admin@mozilla.com'))
        res = self.client.post(self.list_url,
                               body=json.dumps({'manifest': obj.uuid}))
        eq_(res.status_code, 401)
        eq_(self.count(), 0)

    def test_create(self):
        obj = self.create()
        res = self.client.post(self.list_url,
                               body=json.dumps({'manifest': obj.uuid}))
        eq_(res.status_code, 201)
        content = json.loads(res.content)
        eq_(content['status'], 0)
        eq_(content['slug'], u'mozillaball')
        eq_(content['support_email'], None)
        eq_(self.count(), 1)

        app = Webapp.objects.get(app_slug=content['slug'])
        eq_(set(app.authors.all()), set([self.user]))

    def create_app(self):
        obj = self.create()
        res = self.client.post(self.list_url,
                               body=json.dumps({'manifest': obj.uuid}))
        pk = json.loads(res.content)['id']
        self.get_url = ('api_dispatch_detail',
                        {'resource_name': 'app', 'pk': pk})
        return Webapp.objects.get(pk=pk)

    def test_get(self):
        self.create_app()
        res = self.client.get(self.get_url)
        eq_(res.status_code, 200)
        content = json.loads(res.content)
        eq_(content['status'], 0)

    def test_get_not_mine(self):
        obj = self.create_app()
        obj.authors.clear()
        res = self.client.get(self.get_url)
        eq_(res.status_code, 401)


@patch.object(settings, 'SITE_URL', 'http://api/')
class TestCategoryHandler(BaseOAuth):

    def setUp(self):
        super(TestCategoryHandler, self).setUp()
        self.cat = Category.objects.create(name='Webapp',
                                           type=amo.ADDON_WEBAPP)
        self.cat.name = {'fr': 'Le Webapp'}
        self.cat.save()
        self.other = Category.objects.create(name='other',
                                             type=amo.ADDON_EXTENSION)

        self.list_url = ('api_dispatch_list', {'resource_name': 'category'})
        self.get_url = ('api_dispatch_detail',
                        {'resource_name': 'category', 'pk': self.cat.pk})

    def test_verbs(self):
        self._allowed_verbs(self.list_url, ['get'])
        self._allowed_verbs(self.get_url, ['get'])

    def test_get_categories(self):
        res = self.client.get(self.list_url)
        data = json.loads(res.content)
        eq_(data['meta']['total_count'], 1)
        eq_(data['objects'][0]['name'], 'Webapp')

    def test_get_category(self):
        res = self.client.get(self.get_url)
        data = json.loads(res.content)
        eq_(data['name'], 'Webapp')

    def test_get_category_localised(self):
        with self.activate(locale='fr'):
            res = self.client.get(self.get_url)
            data = json.loads(res.content)
            eq_(data['name'], 'Le Webapp')

    def test_get_other_category(self):
        res = self.client.get(('api_dispatch_detail',
                              {'resource_name': 'category',
                               'pk': self.other.pk}))
        eq_(res.status_code, 404)
