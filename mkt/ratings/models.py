import logging
from datetime import datetime, timedelta

from django.core.cache import cache
from django.db import models

import bleach
from celeryutils import task
from tower import ugettext_lazy as _

import amo.models
from mkt.translations.fields import save_signal, TranslatedField
from mkt.users.models import UserProfile


log = logging.getLogger('z.review')


class ReviewManager(amo.models.ManagerBase):

    def valid(self):
        """Get all reviews that aren't replies."""
        # Use extra because Django wants to do a LEFT OUTER JOIN.
        return self.extra(where=['reply_to IS NULL'])


class Review(amo.models.ModelBase):
    addon = models.ForeignKey('webapps.Addon', related_name='_reviews')
    version = models.ForeignKey('versions.Version', related_name='reviews',
                                null=True)
    user = models.ForeignKey('users.UserProfile', related_name='_reviews_all')
    reply_to = models.ForeignKey('self', null=True, unique=True,
                                 related_name='replies', db_column='reply_to')

    rating = models.PositiveSmallIntegerField(null=True)
    title = TranslatedField(require_locale=False)
    body = TranslatedField(require_locale=False)
    ip_address = models.CharField(max_length=255, default='0.0.0.0')

    editorreview = models.BooleanField(default=False)
    flag = models.BooleanField(default=False)
    sandbox = models.BooleanField(default=False)

    # Denormalized fields for easy lookup queries.
    # TODO: index on addon, user, latest
    is_latest = models.BooleanField(
        default=True, editable=False,
        help_text="Is this the user's latest review for the add-on?")
    previous_count = models.PositiveIntegerField(
        default=0, editable=False,
        help_text="How many previous reviews by the user for this add-on?")

    objects = ReviewManager()

    class Meta:
        db_table = 'reviews'
        ordering = ('-created',)

    def get_url_path(self):
        return '/app/%s/ratings/%s' % (self.addon.app_slug, self.id)

    @classmethod
    def get_replies(cls, reviews):
        reviews = [r.id for r in reviews]
        qs = Review.objects.filter(reply_to__in=reviews)
        return dict((r.reply_to_id, r) for r in qs)

    @staticmethod
    def post_save(sender, instance, created, **kwargs):
        if kwargs.get('raw'):
            return
        instance.refresh(update_denorm=created)
        if created:
            # Avoid slave lag with the delay.
            check_spam.apply_async(args=[instance.id], countdown=600)

    @staticmethod
    def post_delete(sender, instance, **kwargs):
        if kwargs.get('raw'):
            return
        instance.refresh(update_denorm=True)

    def refresh(self, update_denorm=False):
        from . import tasks

        if update_denorm:
            pair = self.addon_id, self.user_id
            # Do this immediately so is_latest is correct. Use default
            # to avoid slave lag.
            tasks.update_denorm(pair, using='default')

        # Review counts have changed, so run the task and trigger a reindex.
        tasks.addon_review_aggregates.delay(self.addon_id, using='default')

    @staticmethod
    def transformer(reviews):
        user_ids = dict((r.user_id, r) for r in reviews)
        for user in UserProfile.objects.no_cache().filter(id__in=user_ids):
            user_ids[user.id].user = user


models.signals.post_save.connect(Review.post_save, sender=Review,
                                 dispatch_uid='review_post_save')
models.signals.post_delete.connect(Review.post_delete, sender=Review,
                                   dispatch_uid='review_post_delete')
models.signals.pre_save.connect(save_signal, sender=Review,
                                dispatch_uid='review_translations')


# TODO: translate old flags.
class ReviewFlag(amo.models.ModelBase):
    SPAM = 'review_flag_reason_spam'
    LANGUAGE = 'review_flag_reason_language'
    SUPPORT = 'review_flag_reason_bug_support'
    OTHER = 'review_flag_reason_other'
    FLAGS = (
        (SPAM, _(u'Spam or otherwise non-review content')),
        (LANGUAGE, _(u'Inappropriate language/dialog')),
        (SUPPORT, _(u'Misplaced bug report or support request')),
        (OTHER, _(u'Other (please specify)')),
    )

    review = models.ForeignKey(Review)
    user = models.ForeignKey('users.UserProfile', null=True)
    flag = models.CharField(max_length=64, default=OTHER,
                            choices=FLAGS, db_column='flag_name')
    note = models.CharField(max_length=100, db_column='flag_notes', blank=True,
                            default='')

    class Meta:
        db_table = 'reviews_moderation_flags'
        unique_together = (('review', 'user'),)


class Spam(object):

    def add(self, review, reason):
        reason = 'mkt:review:spam:%s' % reason
        try:
            reasonset = cache.get('mkt:review:spam:reasons', set())
        except KeyError:
            reasonset = set()
        try:
            idset = cache.get(reason, set())
        except KeyError:
            idset = set()
        reasonset.add(reason)
        cache.set('mkt:review:spam:reasons', reasonset)
        idset.add(review.id)
        cache.set(reason, idset)
        return True

    def reasons(self):
        return cache.get('mkt:review:spam:reasons')


@task
def check_spam(review_id, **kw):
    spam = Spam()
    try:
        review = Review.objects.using('default').get(id=review_id)
    except Review.DoesNotExist:
        log.error('Review does not exist, check spam for review_id: %s'
                  % review_id)
        return

    thirty_days = datetime.now() - timedelta(days=30)
    others = (Review.objects.no_cache().exclude(id=review.id)
              .filter(user=review.user, created__gte=thirty_days))
    if len(others) > 10:
        spam.add(review, 'numbers')
    if (review.body is not None and
        bleach.url_re.search(review.body.localized_string)):
        spam.add(review, 'urls')
    for other in others:
        if ((review.title and review.title == other.title) or
            review.body == other.body):
            spam.add(review, 'matches')
            break
