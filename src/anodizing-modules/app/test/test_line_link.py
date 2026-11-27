from anodizing_web.line_link import LineLink, line_carrier


def test_line_link_reports_loss_changes_diagnosis_and_recovers():
    carrier = True
    link = LineLink(lambda: carrier)

    link.sample(True, 0)
    assert link.state['state'] == 'ok'
    assert link.state['since'] is None

    carrier = False
    link.sample(False, 5)
    loss = link.state
    assert loss['state'] == 'lost'
    assert loss['since']
    assert loss['cause'] == 'adapter_no_link'
    assert 'USB Wi-Fi dongle' in loss['hint']

    carrier = True
    link.sample(False, 6)
    assert link.state == loss
    link.sample(False, 10)
    assert link.state['cause'] == 'executor_not_answering'
    assert link.state['since'] == loss['since']
    assert 'powered on' in link.state['hint']

    link.sample(True, 15)
    recovery = link.state
    assert recovery['state'] == 'ok'
    assert recovery['cause'] is None
    assert recovery['since']
    link.sample(True, 20)
    assert link.state == recovery


def test_unknown_carrier_uses_executor_reachability():
    link = LineLink(lambda: None)
    link.sample(None, 0)
    assert link.state is None
    link.sample(False, 5)
    assert link.state['cause'] == 'executor_not_answering'
    link.sample(True, 10)
    assert link.state['state'] == 'ok'


def test_selects_the_ipc_route_and_remembers_it_when_the_adapter_disappears(tmp_path):
    routes = tmp_path / 'route'
    # The documentation subnet uses the line interface. The default uses another adapter.
    default = 'uplink 00000000 00000000 0003 0 0 0 00000000 0 0 0\n'
    line = 'linewifi 006433C6 00000000 0001 0 0 100 00FFFFFF 0 0 0\n'
    routes.write_text('Iface Destination Gateway Flags RefCnt Use Metric Mask MTU Window IRTT\n' + default + line)
    interfaces = tmp_path / 'net'
    adapter = interfaces / 'linewifi'
    adapter.mkdir(parents=True)
    (adapter / 'carrier').write_text('1\n')
    read_carrier = line_carrier('198.51.100.42', routes, interfaces)
    assert read_carrier() is True
    (adapter / 'carrier').unlink()
    (adapter / 'operstate').write_text('down\n')
    assert read_carrier() is False
    (adapter / 'operstate').unlink()
    adapter.rmdir()
    routes.write_text('Iface Destination Gateway Flags RefCnt Use Metric Mask MTU Window IRTT\n' + default)
    assert read_carrier() is False
