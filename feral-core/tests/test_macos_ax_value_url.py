"""Inert real skill paths; no live AX, personal windows or input effects."""
import json
from types import SimpleNamespace

import pytest

from skills.impl import macos_ax as ax

URL = 'http://127.0.0.1:9876/synthetic-review?view=fixture#section'


class Element:
    def __init__(self, name, **attributes):
        self.name = name
        self.attributes = attributes


@pytest.fixture
def tree(monkeypatch):
    address = Element('address', AXRole='AXTextField', AXTitle='Address and search bar', AXValue=URL)
    document = Element('document', AXRole='AXWebArea', AXTitle='Synthetic page', AXURL=URL)
    secure = Element('secure', AXRole='AXTextField', AXSubrole='AXSecureTextField',
                     AXTitle='Password', AXValue='synthetic-never-read', AXURL='synthetic-never-read')
    window = Element('window', AXRole='AXWindow', AXTitle='Synthetic owned window', AXChildren=[address, document, secure])
    other = Element('other', AXRole='AXWindow', AXTitle='Other synthetic window', AXChildren=[])
    app_element = Element('app', AXRole='AXApplication', AXWindows=[other, window])
    reads = []

    def attribute(element, name):
        reads.append((element.name, name))
        if element is secure and name in {'AXValue', 'AXURL'}:
            raise AssertionError('Secure value/URL attribute must never be read')
        return (0, element.attributes[name]) if name in element.attributes else (-25205, None)

    monkeypatch.setattr(ax, '_attr', attribute)
    monkeypatch.setattr(ax, '_resolve_app', lambda name: (ax._App('Synthetic Chrome', 4321, 'synthetic.chrome'), ''))
    monkeypatch.setattr(ax, '_app_element', lambda app: app_element)
    monkeypatch.setattr(ax, '_actions', lambda element: ['AXPress'] if element is address else [])
    monkeypatch.setattr(ax, '_bounds', lambda element: {'x': 1, 'y': 2, 'width': 3, 'height': 4})
    monkeypatch.setattr(ax, 'accessibility_trusted', lambda: True)
    monkeypatch.setattr(ax, '_REFS', ax._RefStore())
    monkeypatch.setattr(ax, '_ax', lambda: SimpleNamespace(
        AXUIElementCopyAttributeNames=lambda element, unused: (0, list(element.attributes)),
        AXUIElementIsAttributeSettable=lambda element, name, unused: (0, False),
    ))
    return SimpleNamespace(address=address, document=document, secure=secure,
                           window=window, other=other, reads=reads, skill=ax.MacOSAccessibilitySkill())


def test_snapshot_retains_label_and_exposes_named_address_value(tree):
    result = tree.skill._snapshot({'app': 'Synthetic Chrome', 'window_index': 1, 'filter': 'all'})
    assert result['success']
    node = next(n for n in result['data']['nodes'] if n['label'] == 'Address and search bar')
    assert node['value'] == URL
    assert node['window_index'] == 1
    assert node['label_source'] == 'AXTitle'
    assert URL in result['data']['tree']
    assert ax._REFS.get(node['ref']).element is tree.address
    document = next(n for n in result['data']['nodes'] if n['role'] == 'AXWebArea')
    assert document['url'] == URL


@pytest.mark.parametrize('query,role', [(URL, 'AXTextField'), ('synthetic-review', 'AXTextField'), ('127.0.0.1', 'AXWebArea')])
def test_find_searches_value_url_without_losing_exact_element(tree, query, role):
    result = tree.skill._find({'query': query, 'window_index': 1, 'filter': 'all', 'include_menus': False})
    assert result['success']
    row = next(n for n in result['data']['matches'] if n['role'] == role)
    assert row.get('value', row.get('url')) == URL
    assert row['window_index'] == 1
    assert ax._REFS.get(row['ref']).element is (tree.address if role == 'AXTextField' else tree.document)


def test_describe_reads_current_value_for_same_ref(tree):
    node = ax._describe_node(tree.address, 1)
    ref = ax._REFS.add(tree.address, node, 'Synthetic Chrome', 4321)
    tree.address.attributes['AXValue'] = URL + '-updated'
    result = tree.skill._describe({'ref': ref})
    assert result['success'] and result['data']['value'] == URL + '-updated'
    assert result['data']['ref'] == ref and result['data']['label'] == 'Address and search bar'


@pytest.mark.parametrize('role,subrole', [('AXSecureTextField', ''), ('AXTextField', 'AXSecureTextField')])
def test_secure_role_and_subrole_never_read_sensitive_attributes(tree, role, subrole):
    tree.secure.attributes.update(AXRole=role, AXSubrole=subrole)
    node = ax._describe_node(tree.secure, 1)
    assert node.secure and not node.to_dict().get('value') and not node.to_dict().get('url')
    assert ('secure', 'AXValue') not in tree.reads and ('secure', 'AXURL') not in tree.reads
    assert 'synthetic-never-read' not in node.to_line()
    ref = ax._REFS.add(tree.secure, node, 'Synthetic Chrome', 4321)
    assert tree.skill._describe({'ref': ref})['status_code'] == 403


def test_secure_serialization_withholds_injected_value_url():
    node = ax.AXNode(ref='ax1', role='AXTextField', subrole='AXSecureTextField', label='Password',
                     label_source='AXTitle', depth=1, value='never-print', url='never-print')
    assert 'never-print' not in json.dumps(node.to_dict())
    assert 'never-print' not in node.to_line()


def test_value_url_are_bounded_with_explicit_truncation(tree):
    tree.address.attributes['AXValue'] = 'x' * 9000
    tree.document.attributes['AXURL'] = 'https://invalid.example/' + 'x' * 9000
    value = ax._describe_node(tree.address, 1).to_dict()
    url = ax._describe_node(tree.document, 1).to_dict()
    assert len(value['value']) == ax.MAX_AX_TEXT_CHARS and value['value_truncated'] is True
    assert len(url['url']) == ax.MAX_AX_TEXT_CHARS and url['url_truncated'] is True


def test_text_line_escapes_control_characters(tree):
    tree.address.attributes['AXValue'] = 'Synthetic\nvalue\t"quoted"'
    node = ax._describe_node(tree.address, 1)
    assert '\n' not in node.to_line() and '\t' not in node.to_line()
    assert '\\n' in node.to_line() and '\\t' in node.to_line()


def test_stale_role_refusal_precedes_new_metadata_reads(tree):
    ref = ax._REFS.add(tree.address, ax._describe_node(tree.address, 1), 'Synthetic Chrome', 4321)
    tree.reads.clear()
    tree.address.attributes['AXRole'] = 'AXButton'
    assert tree.skill._describe({'ref': ref})['status_code'] == 409
    assert ('address', 'AXValue') not in tree.reads and ('address', 'AXURL') not in tree.reads


def test_identical_url_in_another_window_cannot_replace_the_selected_ref(tree):
    other_address = Element('other-address', AXRole='AXTextField', AXTitle='Address and search bar', AXValue=URL)
    tree.other.attributes['AXChildren'] = [other_address]
    result = tree.skill._find({'query': URL, 'window_index': 1, 'filter': 'all', 'include_menus': False})
    assert result['success'] and result['data']['match_count'] == 2
    assert all(row['window_index'] == 1 for row in result['data']['matches'])
    assert all(ax._REFS.get(row['ref']).element is not other_address for row in result['data']['matches'])


@pytest.mark.parametrize('number,expected', [(0, '0'), (1.25, '1.25'), (float('nan'), None), (float('inf'), None)])
def test_numeric_values_are_display_data_and_nonfinite_values_are_omitted(tree, number, expected):
    tree.address.attributes['AXValue'] = number
    assert ax._describe_node(tree.address, 1).to_dict().get('value') == expected


def test_unsupported_attributes_do_not_turn_a_valid_description_into_failure(tree):
    tree.address.attributes.pop('AXValue')
    node = ax._describe_node(tree.address, 1)
    ref = ax._REFS.add(tree.address, node, 'Synthetic Chrome', 4321)
    result = tree.skill._describe({'ref': ref})
    assert result['success'] and result['data']['label'] == 'Address and search bar'
    assert 'value' not in result['data'] and 'url' not in result['data']


def test_real_nsurl_is_supported_without_description_fallback(tree):
    foundation = pytest.importorskip('Foundation')
    tree.document.attributes['AXURL'] = foundation.NSURL.URLWithString_(URL)
    assert ax._describe_node(tree.document, 1).to_dict()['url'] == URL


def test_opaque_values_are_not_stringified(tree):
    class Opaque:
        def __str__(self):
            raise AssertionError('Do not stringify unsupported AX objects')
    tree.address.attributes['AXValue'] = Opaque()
    tree.document.attributes['AXURL'] = Opaque()
    assert not ax._describe_node(tree.address, 1).to_dict().get('value')
    assert not ax._describe_node(tree.document, 1).to_dict().get('url')
