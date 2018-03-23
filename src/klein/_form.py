# -*- test-case-name: klein.test.test_form -*-

from __future__ import print_function, unicode_literals

from itertools import count
from typing import (
    Any, AnyStr, Callable, Dict, Iterable, List, Optional, TYPE_CHECKING, Text,
    Union, cast
)

import attr

from twisted.internet.defer import inlineCallbacks
from twisted.python.compat import unicode
from twisted.python.components import Componentized, registerAdapter
from twisted.web.error import MissingRenderMethod
from twisted.web.http import FORBIDDEN
from twisted.web.iweb import IRenderable, IRequest
from twisted.web.resource import Resource
from twisted.web.template import Element, Tag, TagLoader, tags

from zope.interface import Interface, implementer

from ._app import _call
from ._decorators import bindable
from .interfaces import (EarlyExit, IDependencyInjector, IRequestLifecycle,
                         IRequiredParameter, ISession, SessionMechanism)

if TYPE_CHECKING:
    from mypy_extensions import DefaultNamedArg, NoReturn
    (Tag, Any, Callable, Dict, Optional, AnyStr, Iterable, IRequest, List,
     Text, DefaultNamedArg, Union, NoReturn)
else:
    def DefaultNamedArg(*ignore):
        pass

class CrossSiteRequestForgery(Resource, object):
    """
    Cross site request forgery detected.  Request aborted.
    """
    def __init__(self, message):
        super(CrossSiteRequestForgery, self).__init__()
        self.message = message

    def render(self, request):
        """
        For all HTTP methods, return a 403.
        """
        request.setResponseCode(FORBIDDEN, b"FAILURECSRF")
        return ("CSRF TOKEN FAILURE: " + self.message).encode("utf-8")

CSRF_PROTECTION = "__csrf_protection__"

class ValidationError(Exception):
    """
    A L{ValidationError} is raised by L{Field.extractValue}.
    """

    def __init__(self, message):
        # type: (str) -> None
        """
        Initialize a L{ValidationError} with a message to show to the user.
        """
        super(ValidationError, self).__init__(message)
        self.message = message


def textConverter(value):
    # type: (AnyStr) -> Text
    """
    Converter for form values (which may be any type of string) into text.
    """
    return (
        value if isinstance(value, unicode) else unicode(value, "utf-8")
    )



@implementer(IDependencyInjector, IRequiredParameter)
@attr.s(frozen=True)
class Field(object):
    """
    A L{Field} is a static part of a L{Form}.

    @ivar converter: The converter.
    """

    converter = attr.ib(type=Callable[[AnyStr], Any])
    formInputType = attr.ib(type=str)
    pythonArgumentName = attr.ib(type=Optional[str], default=None)
    formFieldName = attr.ib(type=Optional[str], default=None)
    formLabel = attr.ib(type=Optional[str], default=None)
    noLabel = attr.ib(type=bool, default=False)
    value = attr.ib(type=Text, default=u"")
    error = attr.ib(type=ValidationError, default=None)
    order = attr.ib(type=int, default=attr.Factory(lambda: next(count())))

    # IRequiredParameter
    def registerInjector(self, injectionComponents, parameterName,
                         requestLifecycle):
        # type: (Componentized, str, RequestLifecycle) -> IDependencyInjector
        """
        Register this form field as a dependency injector.
        """
        protoForm = IProtoForm(injectionComponents)
        return protoForm.addField(self.maybeNamed(parameterName))


    def maybeNamed(self, name):
        # type: (str) -> Field
        """
        Create a new L{Field} like this one, but with all the name default
        values filled in.

        @param name: the name.
        @type name: a native L{str}
        """
        def maybe(it, that=name):
            # type: (Optional[str], Optional[str]) -> Optional[str]
            return that if it is None else it
        return attr.assoc(
            self,
            pythonArgumentName=maybe(self.pythonArgumentName),
            formFieldName=maybe(self.formFieldName),
            formLabel=maybe(self.formLabel,
                            name.capitalize() if not self.noLabel else None),
        )


    def asTags(self):
        # type: () -> Iterable[Tag]
        """
        Convert this L{Field} into some stuff that can be rendered in a
        L{twisted.web.template}.

        @return: A new set of tags to include in a template.
        @rtype: iterable of L{twisted.web.template.Tag}
        """
        input_tag = tags.input(type=self.formInputType,
                               name=self.formFieldName, value=self.value)
        if self.formLabel:
            yield tags.label(self.formLabel, ": ", input_tag)
        else:
            yield input_tag
        if self.error:
            yield tags.div(self.error.message)


    def extractValue(self, request):
        # type: (IRequest) -> Text
        """
        Extract a text value from the request.
        """
        fieldName = self.formFieldName
        if fieldName is None:
            raise ValueError("Cannot extract unnamed form field.")
        return (
            (request.args.get(fieldName.encode("utf-8")) or [b""])[0]
            .decode("utf-8")
        )


    def validateValue(self, value):
        # type: (AnyStr) -> Any
        """
        Validate the given text and return a converted Python object to use, or
        fail with L{ValidationError}.
        """
        return self.converter(value)


    @classmethod
    def text(cls):              # type: () -> Field
        """
        Shorthand for a form field that contains a short string, and will be
        rendered as a plain <input>.
        """
        return cls(converter=textConverter, formInputType="text")

    @classmethod
    def password(cls):          # type: () -> Field
        """
        Shorthand for a form field that, like L{text}, contains a short string,
        but should be obscured when typed (and, to the extent possible,
        obscured in other sensitive contexts, such as logging.)
        """
        return cls(converter=textConverter,
                   formInputType="password")

    @classmethod
    def hidden(cls, name, value):
        # type: (str, Text) -> Field
        """
        Shorthand for a hidden field.
        """
        return cls(converter=textConverter,
                   formInputType="hidden",
                   noLabel=True,
                   value=value).maybeNamed(name)


    @classmethod
    def integer(cls, minimum=None, maximum=None):
        # type: (Optional[int], Optional[int]) -> Field
        """
        An integer within the range [minimum, maximum].
        """
        def bounded_int(text):
            # type: (AnyStr) -> Any
            try:
                value = int(text)
            except ValueError:
                raise ValidationError("must be an integer")
            else:
                if minimum is not None and value < minimum:
                    raise ValidationError("value must be >=" + repr(minimum))
                if maximum is not None and value > maximum:
                    raise ValidationError("value must be <=" + repr(maximum))
                return value
        return cls(converter=bounded_int, formInputType="number")


@implementer(IRenderable)
@attr.s
class RenderableForm(object):
    """
    An L{IRenderable} representing a renderable form.

    @ivar prevalidationValues: a L{dict} mapping {L{Field}: L{list} of
        L{unicode}}, representing the value that each field received as part of
        the request.

    @ivar validationErrors: a L{dict} mapping {L{Field}: L{ValidationError}}
    """
    _form = attr.ib(type='Form')
    _session = attr.ib(type=ISession)
    _action = attr.ib(type=str)
    _method = attr.ib(type=str)
    _enctype = attr.ib(type=str)
    _encoding = attr.ib(type=str)
    prevalidationValues = attr.ib(
        type=Dict[Field, List[Text]],
        default=cast(Dict[Field, List[Text]], attr.Factory(dict))
    )
    validationErrors = attr.ib(
        type=Dict[Field, ValidationError],
        default=cast(Dict[Field, ValidationError], attr.Factory(dict))
    )

    ENCTYPE_FORM_DATA = 'multipart/form-data'
    ENCTYPE_URL_ENCODED = 'application/x-www-form-urlencoded'

    def _fieldForCSRF(self):
        # type: () -> Field
        """
        @return: A hidden L{Field} containing the cross-site request forgery
            protection token.
        """
        return Field.hidden(CSRF_PROTECTION, self._session.identifier)


    def _fieldsToRender(self):
        # type: () -> Iterable[Field]
        """
        @return: an interable of L{Field} objects to include in the HTML
            representation of this form.  This includes:

                - all the user-specified fields in the form

                - the CSRF protection hidden field

                - if no "submit" buttons are included in the form, one
                  additional field for a default submit button so the form can
                  be submitted.
        """
        anySubmit = False
        for field in self._form._fields:
            yield attr.assoc(
                field,
                value=self.prevalidationValues.get(field, field.value),
                error=self.validationErrors.get(field, None)
            )
            if field.formInputType == "submit":
                anySubmit = True
        if not anySubmit:
            yield Field(converter=str, formInputType="submit", value=u"submit",
                        formFieldName="__klein_auto_submit__")
        yield self._fieldForCSRF()

    # Public interface below.

    def lookupRenderMethod(self, name):
        # type: (str) -> NoReturn
        """
        Form renderers don't supply any render methods, so this just always
        raises L{MissingRenderMethod}.
        """
        raise MissingRenderMethod(name)


    def render(self, request):
        # type: (IRequest) -> Tag
        """
        Render this form to the given request.
        """
        return (
            tags.form(action=self._action, method=self._method,
                      enctype=self._enctype,
                      **{"accept-charset": self._encoding})
            (
                field.asTags() for field in self._fieldsToRender()
            )
        )


    def glue(self):
        # type: () -> Iterable[Tag]
        """
        Provide any glue necessary to render this form; this must be dropped
        into the template within the C{<form>} tag.

        Presently, this glue includes only the CSRF token argument, but Klein
        reserves the right to add arbitrary HTML here.  This should not create
        any user-visible content, however.

        @return: some HTML elements in the form of renderable objects for
            L{twisted.web.template}
        @rtype: L{twisted.web.template.Tag}, or L{list} thereof.
        """
        return self._fieldForCSRF().asTags()



@bindable
def defaultValidationFailureHandler(
        instance,               # type: Optional[object]
        request,                # type: IRequest
        form,                   # type: Form
        prevalidationValues,    # type: Dict[Field, List[str]]
        validationErrors        # type: Dict[Field, ValidationError]
):
    # type: (...) -> Element
    """
    This is the default validation failure handler, which will be used by
    L{Form.handler} in the case of any input validation failure when no other
    validation failure handler is registered via
    L{Form.onValidationFailureFor}.

    Its behavior is to simply return an HTML rendering of the form object,
    which includes inline information about fields which failed to validate.

    @param instance: The instance associated with the router that the form
        handler was handled on.
    @type instance: L{object}

    @param request: The request including the form submission.
    @type request: L{twisted.web.iweb.IRequest}

    @param renderable: The form, including any unrendered fields.
    @type renderable: L{RenderableForm}

    @return: Any object acceptable from a Klein route.
    """
    session = request.getComponent(ISession)
    renderable = RenderableForm(
        form, session, u"/".join(
            segment.decode("utf-8", errors='replace')
            for segment in request.prepath
        ),
        request.method,
        request.getHeader(b'content-type').split(b';')[0]
        .decode("charmap"),
        "utf-8", prevalidationValues, validationErrors,
    )

    return Element(TagLoader(renderable))

class _HandlerTypeStub(object):
    """
    A type stub for a form handler (not to be confused with a validation error
    handler).
    """
    __kleinFormValidationFailureHandlers__ = (
        None
    )                           # type: Dict[Form, _validationFailureHandler]

    def __call__(self, request, *a, **kw):
        # type: (IRequest, *Any, **Any) -> Any
        """
        Takes a request, and other arguments that are defined at a higher
        level.
        """

_routeCallable = Any
_routeDecorator = Callable[
    [_routeCallable, DefaultNamedArg(Any, '__session__')],
    _routeCallable
]
_validationFailureHandler = Callable[
    [Optional[object], IRequest, 'Form', Dict[str, str]], Element
]

validationFailureHandlerAttribute = "__kleinFormValidationFailureHandlers__"


class IProtoForm(Interface):
    """
    Marker interface for L{ProtoForm}.
    """

class IForm(Interface):
    """
    Marker interface for form attached to dependency injection components.
    """


@implementer(IProtoForm)
@attr.s
class ProtoForm(object):
    """
    Form-builder.
    """
    _componentized = attr.ib(type=Componentized)
    _lifecycle = attr.ib(type='RequestLifecycle')
    _fields = attr.ib(type=List[Field], default=attr.Factory(list))

    @classmethod
    def fromComponentized(cls, componentized):
        """
        Create a ProtoForm from a componentized object.
        """
        rl = IRequestLifecycle(componentized)
        assert rl is not None
        return cls(componentized, rl)


    def addField(self, field):
        """
        Add the given field to the form ultimately created here.
        """
        self._fields.append(field)
        return FieldInjector(self._componentized, field, self._lifecycle)


class IFieldValues(Interface):
    """
    Marker interface for parsed fields.
    """

@implementer(IFieldValues)
@attr.s
class FieldValues(object):
    """
    Reified post-parsing values for HTTP form submission.
    """

    arguments = attr.ib()
    prevalidationValues = attr.ib()
    validationErrors = attr.ib()

    @inlineCallbacks
    def validate(self, instance, request):
        """

        """
        if self.validationErrors:
            result = yield _call(
                instance,
                IValidationFailureHandler(self._injectionComponents),
                request, self, self.prevalidationValues, self.validationErrors
            )
            raise EarlyExit(result)





@implementer(IDependencyInjector)
@attr.s
class FieldInjector(object):
    """
    Field injector.
    """
    _componentized = attr.ib()
    _field = attr.ib()
    _lifecycle = attr.ib()

    def injectValue(self, request):
        # type: (request) -> Any
        """
        Inject the given value into the form.
        """
        return IFieldValues(request).arguments[self._field.pythonArgumentName]

    def finalize(self):
        """
        Finalize this ProtoForm into a real form.
        """
        finalForm = IForm(self._componentized, None)
        if finalForm is not None:
            return
        finalForm = Form(IProtoForm(self._componentized)._fields)
        self._componentized.setComponent(IForm, finalForm)

        # XXX set requiresComponents argument here to ISession if CSRF is
        # required; add flag somewhere to indicate if a form is
        # side-effect-free (like a search field) that can be handled even
        # without a CSRF token.
        self._lifecycle.addBeforeHook(
            finalForm.populateRequestValues, provides=[IFieldValues],
            requires=[ISession]
        )



registerAdapter(ProtoForm.fromComponentized, Componentized, IProtoForm)



class IValidationFailureHandler(Interface):
    """
    Validation failure handler callable interface.
    """


def checkCSRF(request):
    """
    Check the request for cross-site request forgery, raising an EarlyExit if
    it is found.
    """
    # TODO: optionalize CSRF protection for GET forms
    session = ISession(request)
    if session.authenticatedBy == SessionMechanism.Cookie:
        token = request.args.get(CSRF_PROTECTION.encode("ascii"),
                                 [b""])[0]
        token = token.decode("ascii")
        if token != session.identifier:
            # leak only the value passed, not the actual token, just in
            # case there's some additional threat vector there
            raise EarlyExit(CrossSiteRequestForgery(
                "CSRF token mismatch: {!r}".format(token)
            ))



@attr.s(hash=False)
class Form(object):
    """
    A L{Form} object which includes an authorizer, and may therefore be bound
    (via L{Form.bind}) to an individual session, producing a L{RenderableForm}.
    """
    _fields = attr.ib(default=cast(List[Field], attr.Factory(list)),
                      type=List[Field])

    @staticmethod
    def onValidationFailureFor(handler):
        # type: (_HandlerTypeStub) -> Callable[[Callable], Callable]
        """
        Register a function to be run in the event of a validation failure for
        the input to a particular form handler.

        Generally used like so::

            myForm = Form(...).authorizedUsing(...)
            router = Klein()
            @myForm.handler(router.route("/", methods=['POST']))
            def handleForm(request, ...):
                ...

            # Handle input validation failures for handleForm
            @myForm.onValidationFailureFor(handleForm)
            def handleValidationFailures(request, formWithErrors,
                                         values, errors):
                return "Your inputs didn't validate."

        @see: L{defaultValidationFailureHandler} for a more detailed
            description of the decorated function's expected signature.

        @param handler: The form handler - i.e. function decorated by
            L{Form.handler} - for which the decorated function will handle
            validation failures.

        @return: a decorator that decorates a function with the signature
            C{(request, form) -> thing klein can render}.
        """
        def decorate(decoratee):
            # type: (Callable) -> Callable
            handler.injectionComponents.setComponent(IValidationFailureHandler,
                                                     decoratee)
            return decoratee
        return decorate


    @bindable
    def populateRequestValues(self, instance, request):
        """
        Extract the values present in this request and populate a
        L{FieldValues} object.
        """
        assert IFieldValues(request, None) is None

        validationErrors = {}
        prevalidationValues = {}
        arguments = {}

        checkCSRF(request)

        for field in self._fields:
            text = field.extractValue(request)
            prevalidationValues[field] = text
            try:
                value = field.validateValue(text)
                argName = field.pythonArgumentName
                if argName is None:
                    raise ValidationError(
                        "Form fields must all have names."
                    )
            except ValidationError as ve:
                validationErrors[field] = ve
            else:
                arguments[argName] = value
        values = FieldValues(arguments, prevalidationValues, validationErrors)
        request.setComponent(IFieldValues, values)


    @staticmethod
    def rendererFor(
            decoratedFunction,
            action,             # type: Union[Text, bytes]
            method=u"POST",     # type: Union[Text, bytes]
            enctype=RenderableForm.ENCTYPE_FORM_DATA,  # type: Text
            argument="form",                           # type: str
            encoding="utf-8"                           # type: str
    ):
        """
        A form parameter that can render a form declared as a number of fields
        on another route.

        Use like so::

            class MyFormApp(object):
                router = Klein()
                requirer = Requirer()

                @requirer.require(
                    router.route("/handle-form", methods=["POST"]),
                    name=Field.text(), value=Field.integer(),
                )
                def formRoute(self, request, name, value):
                    ...

                @requirer.require(
                    router.route("/show-form", methods=["GET"]),
                    form=Form.rendererFor(formRoute)
                )
                def showForm(self, request, form):
                    return form

        As a L{RenderableForm} provides L{IRenderable}, you may return the
        parameter directly
        """
        form = IForm(decoratedFunction.injectionComponents, None)
        if form is None:
            form = Form([])
        return RenderableFormParam(form, action, method, enctype, encoding)


@implementer(IRequiredParameter, IDependencyInjector)
@attr.s
class RenderableFormParam(object):
    """
    A L{RenderableFormParam} implements L{IRequiredParameter} and
    L{IDependencyInjector} to provide a L{RenderableForm} to your route.
    """

    _form = attr.ib()
    _action = attr.ib()
    _method = attr.ib()
    _enctype = attr.ib()
    _encoding = attr.ib()

    def registerInjector(self, injectionComponents, parameterName,
                         requestLifecycle):
        return self

    def injectValue(self, request):
        """
        Create the renderable form from the request.
        """
        return RenderableForm(
            self._form, ISession(request), self._action, self._method,
            self._enctype, self._encoding, prevalidationValues={},
            validationErrors={}
        )

    def finalize(self):
        """
        Nothing to do upon finalization.
        """
