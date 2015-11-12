"""
Since django 1.8.x, django comes with native multiple template engine support.
It also comes with jinja2 backend, but it is slightly unflexible, and it does
not support by default all django filters and related stuff.

This is an implementation of django backend inteface for use
django_jinja easy with django 1.8.
"""

from __future__ import absolute_import

import copy
import sys
from importlib import import_module

import jinja2
from django.conf import settings
from django.core import signals
from django.core.exceptions import ImproperlyConfigured
from django.dispatch import receiver
from django.middleware.csrf import get_token
from django.template import RequestContext
from django.template import TemplateDoesNotExist
from django.template import TemplateSyntaxError
from django.template.backends.base import BaseEngine
from django.template.backends.utils import csrf_input_lazy
from django.template.backends.utils import csrf_token_lazy
from django.utils import lru_cache
from django.utils import six
from django.utils.encoding import smart_text
from django.utils.functional import SimpleLazyObject
from django.utils.functional import cached_property
from django.utils.module_loading import import_string

from . import base
from . import builtins
from . import utils


class Template(object):
    def __init__(self, template, backend):
        self.template = template
        self.backend = backend
        self._debug = False

    def render(self, context=None, request=None):
        if context is None:
            context = {}

        if request is not None:
            def _get_val():
                token = get_token(request)
                if token is None:
                    return 'NOTPROVIDED'
                else:
                    return smart_text(token)

            context["request"] = request
            context["csrf_token"] = SimpleLazyObject(_get_val)

            # Support for django context processors
            for processor in self.backend.context_processors:
                context.update(processor(request))

        if self._debug:
            from django.test import signals
            from django.template.context import BaseContext

            # Define a "django" like context for emitatet the multi
            # layered context object. This is mainly for apps like
            # django-debug-toolbar that are very coupled to django's
            # internal implementation of context.

            if not isinstance(context, BaseContext):
                class CompatibilityContext(dict):
                    @property
                    def dicts(self):
                        return [self]

                context = CompatibilityContext(context)

            signals.template_rendered.send(sender=self, template=self,
                                           context=context)


        return self.template.render(context)


class Jinja2(BaseEngine):
    app_dirname = "templates"

    @staticmethod
    @lru_cache.lru_cache()
    def get_default():
        """
        When only one django-jinja backend is configured, returns it.
        Raises ImproperlyConfigured otherwise.

        This is required for finding the match extension where the
        developer does not specify a template_engine on a
        TemplateResponseMixin subclass.
        """
        from django.template import engines

        jinja_engines = [engine for engine in engines.all()
                         if isinstance(engine, Jinja2)]
        if len(jinja_engines) == 1:
            # Unwrap the Jinja2 engine instance.
            return jinja_engines[0]
        elif len(jinja_engines) == 0:
            raise ImproperlyConfigured(
                "No Jinja2 backend is configured.")
        else:
            raise ImproperlyConfigured(
                "Several Jinja2 backends are configured. "
                "You must select one explicitly.")

    def __init__(self, params):
        params = params.copy()
        options = params.pop("OPTIONS", {}).copy()

        self.app_dirname = options.pop("app_dirname", "templates")
        super(Jinja2, self).__init__(params)

        newstyle_gettext = options.pop("newstyle_gettext", True)
        context_processors = options.pop("context_processors", [])
        match_extension = options.pop("match_extension", ".jinja")
        match_regex = options.pop("match_regex", None)
        environment_clspath = options.pop("environment", "jinja2.Environment")
        extra_filters = options.pop("filters", {})
        extra_tests = options.pop("tests", {})
        extra_globals = options.pop("globals", {})
        extra_constants = options.pop("constants", {})
        translation_engine = options.pop("translation_engine", "django.utils.translation")

        self.tmpl_debug = options.pop("debug", False)

        undefined = options.pop("undefined", None)
        if undefined is not None:
            if isinstance(undefined, six.string_types):
                options["undefined"] = utils.load_class(undefined)
            else:
                options["undefined"] = undefined

        if settings.DEBUG:
            options.setdefault("undefined", jinja2.DebugUndefined)
        else:
            options.setdefault("undefined", jinja2.Undefined)

        environment_cls = import_string(environment_clspath)

        options.setdefault("loader", jinja2.FileSystemLoader(self.template_dirs))
        options.setdefault("extensions", builtins.DEFAULT_EXTENSIONS)
        options.setdefault("auto_reload", settings.DEBUG)
        options.setdefault("autoescape", True)

        self.env = environment_cls(**options)

        self._context_processors = context_processors
        self._match_regex = match_regex
        self._match_extension = match_extension

        # Initialize i18n support
        if settings.USE_I18N:
            translation = import_module(translation_engine)
            self.env.install_gettext_translations(translation, newstyle=newstyle_gettext)
        else:
            self.env.install_null_translations(newstyle=newstyle_gettext)

        self._initialize_builtins(filters=extra_filters,
                                  tests=extra_tests,
                                  globals=extra_globals,
                                  constants=extra_constants)

        base._initialize_thirdparty(self.env)
        base._initialize_bytecode_cache(self.env)

    def _initialize_builtins(self, filters=None, tests=None, globals=None, constants=None):
        def insert(data, name, value):
            if isinstance(value, six.string_types):
                data[name] = import_string(value)
            else:
                data[name] = value

        if filters:
            for name, value in filters.items():
                insert(self.env.filters, name, value)

        if tests:
            for name, value in tests.items():
                insert(self.env.tests, name, value)

        if globals:
            for name, value in globals.items():
                insert(self.env.globals, name, value)

        if constants:
            for name, value in constants.items():
                self.env.globals[name] = value

    @cached_property
    def context_processors(self):
        return tuple(import_string(path) for path in self._context_processors)

    @property
    def match_extension(self):
        return self._match_extension

    def from_string(self, template_code):
        return Template(self.env.from_string(template_code), self)

    def match_template(self, template_name):
        return base.match_template(template_name,
                                   regex=self._match_regex,
                                   extension=self._match_extension)

    def get_template(self, template_name):
        if not self.match_template(template_name):
            raise TemplateDoesNotExist("Template {} does not exists".format(template_name))

        try:
            template = Template(self.env.get_template(template_name), self)
            template._debug = self.tmpl_debug
            return template
        except jinja2.TemplateNotFound as exc:
            six.reraise(TemplateDoesNotExist, TemplateDoesNotExist(exc.args), sys.exc_info()[2])
        except jinja2.TemplateSyntaxError as exc:
            six.reraise(TemplateSyntaxError, TemplateSyntaxError(exc.args), sys.exc_info()[2])

@receiver(signals.setting_changed)
def _setting_changed(sender, setting, *args, **kwargs):
    """ Reset the Jinja2.get_default() cached when TEMPLATES changes. """
    if setting == "TEMPLATES":
        Jinja2.get_default.cache_clear()
