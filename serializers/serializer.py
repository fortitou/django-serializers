from decimal import Decimal
from django.db.models.fields import FieldDoesNotExist
from django.utils.datastructures import SortedDict
import copy
import datetime
import types
from serializers.renderers import (
    JSONRenderer,
    YAMLRenderer,
    XMLRenderer,
    HTMLRenderer,
    CSVRenderer,
    DumpDataXMLRenderer
)
from serializers.fields import *
from serializers.utils import (
    DictWithMetadata,
    SortedDictWithMetadata,
    is_simple_callable
)
from StringIO import StringIO


def _is_protected_type(obj):
    """
    True if the object is a native datatype that does not need to
    be serialized further.
    """
    return isinstance(obj, (
        types.NoneType,
       int, long,
       datetime.datetime, datetime.date, datetime.time,
       float, Decimal,
       basestring)
    )


def _remove_items(seq, exclude):
    """
    Remove duplicates and items in 'exclude' from list (preserving order).
    """
    seen = set()
    result = []
    for item in seq:
        if (item in seen) or (item in exclude):
            continue
        seen.add(item)
        result.append(item)
    return result


def _get_declared_fields(bases, attrs):
    """
    Create a list of serializer field instances from the passed in 'attrs',
    plus any similar fields on the base classes (in 'bases').

    Note that all fields from the base classes are used.
    """
    fields = [(field_name, attrs.pop(field_name))
              for field_name, obj in attrs.items()
              if isinstance(obj, Field)]
    fields.sort(key=lambda x: x[1].creation_counter)

    # If this class is subclassing another Serializer, add that Serializer's
    # fields.  Note that we loop over the bases in *reverse*. This is necessary
    # in order to the correct order of fields.
    for base in bases[::-1]:
        if hasattr(base, 'base_fields'):
            fields = base.base_fields.items() + fields

    return SortedDict(fields)


def _get_option(name, kwargs, meta, default):
    return kwargs.get(name, getattr(meta, name, default))


class SerializerOptions(object):
    def __init__(self, meta, **kwargs):
        self.format = _get_option('format', kwargs, meta, None)
        self.nested = _get_option('nested', kwargs, meta, False)
        self.include = _get_option('include', kwargs, meta, ())
        self.exclude = _get_option('exclude', kwargs, meta, ())
        self.fields = _get_option('fields', kwargs, meta, ())
        self.include_default_fields = _get_option(
            'include_default_fields', kwargs, meta, True
        )
        self.is_root = _get_option('is_root', kwargs, meta, False)


class ObjectSerializerOptions(SerializerOptions):
    def __init__(self, meta, **kwargs):
        super(ObjectSerializerOptions, self).__init__(meta, **kwargs)
        self.flat_field = _get_option('flat_field', kwargs, meta, Field)
        self.nested_field = _get_option('nested_field', kwargs, meta, None)


class ModelSerializerOptions(SerializerOptions):
    def __init__(self, meta, **kwargs):
        super(ModelSerializerOptions, self).__init__(meta, **kwargs)
        self.model_field_types = _get_option('model_field_types', kwargs, meta, None)
        self.model_field = _get_option('model_field', kwargs, meta, ModelField)
        self.non_model_field = _get_option('non_model_field', kwargs, meta, Field)
        self.related_field = _get_option('related_field', kwargs, meta, PrimaryKeyRelatedField)
        self.nested_related_field = _get_option('nested_related_field', kwargs, meta, None)


class SerializerMetaclass(type):
    def __new__(cls, name, bases, attrs):
        attrs['base_fields'] = _get_declared_fields(bases, attrs)
        return super(SerializerMetaclass, cls).__new__(cls, name, bases, attrs)


class BaseSerializer(Field):
    class Meta(object):
        pass

    renderer_classes = {
        'xml': XMLRenderer,
        'json': JSONRenderer,
        'yaml': YAMLRenderer,
        'csv': CSVRenderer,
        'html': HTMLRenderer,
    }

    _options_class = SerializerOptions
    _use_sorted_dict = True
    internal_use_only = False  # Backwards compatability

    def __init__(self, **kwargs):
        label = kwargs.get('label', None)
        convert = kwargs.get('convert', None)
        super(BaseSerializer, self).__init__(label=label, convert=convert)
        self.kwargs = kwargs
        self.opts = self._options_class(self.Meta, **kwargs)
        self.fields = SortedDict((key, copy.copy(field))
                           for key, field in self.base_fields.items())

        self.has_root_field = False
        for field in self.fields:
            if hasattr(field, 'opts') and getattr(field.opts, 'is_root', None):
                self.has_root_field = True

    def get_flat_serializer(self, obj, field_name):
        raise NotImplementedError()

    def get_nested_serializer(self, obj, field_name):
        raise NotImplementedError()

    def get_default_field_names(self, obj):
        raise NotImplementedError()

    def _get_field_names(self, obj):
        """
        Given an object, return the set of field names to serialize.
        """
        opts = self.opts
        if opts.fields:
            return opts.fields
        else:
            fields = self.fields.keys()
            if opts.include_default_fields or not self.fields:
                fields += self.get_default_field_names(obj)
            fields += list(opts.include)
            return _remove_items(fields, opts.exclude)

    def _get_field_serializer(self, obj, field_name):
        """
        Given an object and a field name, return the serializer instance that
        should be used to serialize that field.
        """
        try:
            return self.fields[field_name]
        except KeyError:
            return self._get_default_field_serializer(obj, field_name)

    def _get_default_field_serializer(self, obj, field_name):
        """
        If a field does not have an explicitly declared serializer, return the
        default serializer instance that should be used for that field.
        """
        if self.opts.nested:
            return self.get_nested_serializer(obj, field_name)
        return self.get_flat_serializer(obj, field_name)

    def get_field_key(self, obj, field_name, field):
        """
        Return the key that should be used for a given field.
        """
        if getattr(field, 'label', None):
            return field.label
        return field_name

    def _convert_field(self, obj, field_name, parent):
        """
        Same behaviour as usual Field, except that we need to keep track
        of state so that we can deal with handling maximum depth and recursion.
        """
        self.parent = parent
        self.root = parent.root or parent
        self.orig_obj = obj
        self.orig_field_name = field_name

        self.stack = parent.stack[:]
        if parent.opts.nested and not isinstance(parent.opts.nested, bool):
            self.opts.nested = parent.opts.nested - 1
        else:
            self.opts.nested = parent.opts.nested

        if self.opts.is_root:
            return self.convert(obj)
        return super(BaseSerializer, self)._convert_field(obj, field_name, parent)

    def convert_object(self, obj):
        if obj in self.stack and not self.opts.is_root:
            serializer = self.get_flat_serializer(self.orig_obj,
                                                  self.orig_field_name)
            return serializer._convert_field(self.orig_obj,
                                             self.orig_field_name,
                                             self)
        self.stack.append(obj)

        if self._use_sorted_dict:
            ret = SortedDictWithMetadata()
        else:
            ret = DictWithMetadata()

        for field_name in self._get_field_names(obj):
            field = self._get_field_serializer(obj, field_name)
            key = self.get_field_key(obj, field_name, field)
            value = field._convert_field(obj, field_name, self)
            ret.set_with_metadata(key, value, field)
        return ret

    def _convert_iterable(self, obj):
        for item in obj:
            yield self.convert(item)

    def convert(self, obj):
        """
        First stage of serialization.  Objects -> Primatives.
        """
        if _is_protected_type(obj):
            return obj
        elif is_simple_callable(obj):
            return self.convert(obj())
        elif isinstance(obj, dict):
            return dict([(key, self.convert(val))
                         for (key, val) in obj.items()])
        elif hasattr(obj, '__iter__'):
            return self._convert_iterable(obj)
        return self.convert_object(obj)

    def render(self, data, stream, format, **opts):
        """
        Second stage of serialization.  Primatives -> Bytestream.
        """
        renderer = self.renderer_classes[format]()
        return renderer.render(data, stream, **opts)

    def serialize(self, obj, format=None, **opts):
        """
        Perform serialization of object into bytestream.
        First converts the objects into primatives, then renders to bytestream.
        """
        self.root = None
        self.stack = []

        data = self.convert(obj)
        format = format or self.opts.format
        if format:
            stream = opts.pop('stream', StringIO())
            self.render(data, stream, format, **opts)
            if hasattr(stream, 'getvalue'):
                self.value = stream.getvalue()
            else:
                self.value = None
        else:
            self.value = data
        return self.value

    def getvalue(self):  # For backwards compatability with existing API.
        return self.value


class Serializer(BaseSerializer):
    __metaclass__ = SerializerMetaclass


class ObjectSerializer(Serializer):
    _options_class = ObjectSerializerOptions

    def get_default_field_names(self, obj):
        """
        Given an object, return the default set of field names to serialize.
        This is what would be serialized if no explicit `Serializer` fields
        are declared.
        """
        return sorted([key for key in obj.__dict__.keys()
                       if not(key.startswith('_'))])

    def get_flat_serializer(self, obj, field_name):
        return self.opts.flat_field()

    def get_nested_serializer(self, obj, field_name):
        return (self.opts.nested_field or self.__class__)()


class ModelSerializer(RelatedField, Serializer):
    """
    A serializer that deals with model instances and querysets.
    """
    _options_class = ModelSerializerOptions

    class Meta:
        related_field = PrimaryKeyRelatedField
        model_field_types = ('pk', 'fields', 'many_to_many')

    def get_default_field_names(self, obj):
        """
        We subclass this method to return the set of all field names defined
        on the model instance, rather than the default behaviour of returning
        all non-private attributes on the object.
        """
        fields = []
        concrete_model = obj._meta.concrete_model

        for field_type in self.opts.model_field_types:
            if field_type == 'pk':
                # Add pk field, descending into inherited pk if needed
                pk_field = concrete_model._meta.pk
                while pk_field.rel:
                    pk_field = pk_field.rel.to._meta.pk
                fields.append(pk_field)
            elif field_type == 'many_to_many':
                # We're explicitly dropping 'through' m2m relations here
                # for the sake of dumpdata compatability.
                # Need to think about what we actually want to do.
                fields.extend([
                    field for field in
                    getattr(concrete_model._meta, field_type)
                    if field.serialize and field.rel.through._meta.auto_created
                ])
            else:
                # Add any non-pk field types
                fields.extend([
                    field for field in
                    getattr(concrete_model._meta, field_type)
                    if field.serialize
                ])
        return [field.name for field in fields]

    def get_flat_serializer(self, obj, field_name):
        """
        We subclass this method to switch between `related_field` and
        `flat_field` depending on the field type.
        """
        try:
            field = obj._meta.get_field_by_name(field_name)[0]
            if isinstance(field, RelatedObject) or field.rel:
                return self.opts.related_field()
            return self.opts.model_field()
        except FieldDoesNotExist:
            return self.opts.non_model_field()

    def get_nested_serializer(self, obj, field_name):
        """
        We subclass this method to switch between `related_field` and
        `flat_field` depending on the field type.
        """
        try:
            field = obj._meta.get_field_by_name(field_name)[0]
            if isinstance(field, RelatedObject) or field.rel:
                return (self.opts.nested_related_field or self.__class__)()
            return self.opts.model_field()
        except FieldDoesNotExist:
            return self.opts.non_model_field()


class DumpDataFields(ModelSerializer):
    _use_sorted_dict = False

    class Meta:
        model_field_types = ('local_fields', 'many_to_many')


class DumpDataSerializer(ModelSerializer):
    """
    A serializer that is intended to produce dumpdata formatted structures.
    """
    _use_sorted_dict = False

    renderer_classes = {
        'xml': DumpDataXMLRenderer,
        'json': JSONRenderer,
        'yaml': YAMLRenderer,
    }

    pk = Field()
    model = ModelNameField()
    fields = DumpDataFields(is_root=True)

    class Meta:
        include_default_fields = False

    def serialize(self, obj, format=None, **opts):
        if opts.get('use_natural_keys', None):
            self.fields['fields'] = DumpDataFields(is_root=True, related_field=NaturalKeyRelatedField, fields=opts.get('fields', None))
        else:
            self.fields['fields'] = DumpDataFields(is_root=True, fields=opts.get('fields', None))

        return super(DumpDataSerializer, self).serialize(obj, format, **opts)


class JSONDumpDataSerializer(DumpDataSerializer):
    class Meta(DumpDataSerializer.Meta):
        format = 'json'


class YAMLDumpDataSerializer(DumpDataSerializer):
    class Meta(DumpDataSerializer.Meta):
        format = 'yaml'


class XMLDumpDataSerializer(DumpDataSerializer):
    class Meta(DumpDataSerializer.Meta):
        format = 'xml'
