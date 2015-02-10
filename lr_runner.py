import errno
from socket import error as socket_error
import logging
from os import environ as env
import re
import string
import StringIO
import time
import uuid

import paramiko as sshclient
from keystoneclient.v2_0 import client as keystoneclient
from novaclient.v1_1 import client as novaclient
from glanceclient.v2 import client as glanceclient
from neutronclient.neutron import client as neutronclient

CONTROLLER_IP = env.get('CONTROLLER_IP')

OS_TENANT_NAME = env.get('OS_TENANT_NAME', 'demo')
OS_USERNAME = env.get('OS_USERNAME', 'admin')
OS_PASSWORD = env.get('OS_PASSWORD', 'pass')
OS_AUTH_URL = env.get('OS_AUTH_URL',
                      'http://{}:5000/v2.0/'.format(CONTROLLER_IP))
OS_REGION_NAME = env.get('OS_REGION', 'RegionOne')
OS_TOKEN = None

LOADRUNNER_IMAGE_NAME = 'data/load_runner.qcow2'
LOADRUNNER_USER = 'ubuntu'

AGENT_IMAGE_NAME = 'data/centos-nettest.qcow2'

SETTINGS_TEMPLATE = 'data/settings.py.template'
SETTINGS_REMOTE_PATH = '/home/ubuntu/load_runner/load_runner/settings.py'
TEST_TEMPLATE = 'data/test.yml.template'
TEST_REMOTE_PATH = '/home/ubuntu/load_runner/load_runner/test.yml'

MANAGEMENT_NET_NAME = env.get('MANAGEMENT_NET_NAME', 'private')
MANAGEMENT_NET_CIDR = env.get('MANAGEMENT_NET_CIDR', '10.0.0.0/24')
MANAGEMENT_NET_ID = env.get('MANAGEMENT_NET_ID')

UBUNTU_IMAGE_URL = 'http://cloud-images.ubuntu.com/trusty/current/trusty-server-cloudimg-amd64-disk1.img'

AGENT_IMAGE_ID = None
AGENT_FLAVOR_ID = None

TEST_NAME = env.get('TEST_NAME', 'lr-test')
TEST_NET_NAME = env.get('TEST_NET_NAME', 'lr-test-net')
IPERF_ARGS = env.get('IPERF_ARGS', "['-t', '15']")

__keystone_client = None
__nova_client = None
__glance_client = None
__neutron_client = None
__ssh_client = None


logging.basicConfig(level=logging.INFO)
UUID=uuid.uuid4().hex


def log_env():
    for k, v in globals().iteritems():
        logging.info('%s=%s', k, v)


def get_keystone_client():
    global __keystone_client
    if not __keystone_client:
        __keystone_client = keystoneclient.Client(
            username=OS_USERNAME,
            password=OS_PASSWORD,
            auth_url=OS_AUTH_URL,
            tenant_name=OS_TENANT_NAME,
            region_name=OS_REGION_NAME
        )
    return __keystone_client


def get_neutron_client():
    global __neutron_client
    if not __neutron_client:
        __neutron_client = neutronclient.Client(
            '2.0', auth_url=OS_AUTH_URL,
            username=OS_USERNAME, password=OS_PASSWORD,
            tenant_name=OS_TENANT_NAME
        )
    return __neutron_client


def get_nova_client():
    global __nova_client
    if not __nova_client:
        __nova_client = novaclient.Client(
            username=OS_USERNAME,
            api_key=OS_PASSWORD,
            auth_url=OS_AUTH_URL,
            project_id=OS_TENANT_NAME,
            region_name=OS_REGION_NAME
        )
    return __nova_client


def get_glance_client():
    global __glance_client
    if not __glance_client:
        keystone = get_keystone_client()
        glance_endpoint = keystone.service_catalog.url_for(service_type='image')

        __glance_client = glanceclient.Client(
            glance_endpoint, token=keystone.auth_token
        )

    return __glance_client


def glance_upload_image(imagename, filename):
    glance = get_glance_client()
    imagename = imagename + '-' + UUID
    logging.info("Uploading '%s' as image '%s'", filename, imagename)
    image = glance.images.create(
        name=imagename,
        disk_format='qcow2',
        container_format='bare')
    with open(filename, 'rb') as fimage:
        glance.images.upload(image.id, fimage)
    logging.info('Uploaded, image_id=%s', image.id)
    return image


def nova_keypair_add():
    keypair_name = 'lr-key-' + UUID
    logging.info("Adding keypair '%s'", keypair_name)
    keypair = get_nova_client().keypairs.create(keypair_name)
    logging.info("Keypair added, id=%s", keypair.id)
    return keypair


def nova_create_secgroup():
    nova = get_nova_client()
    secgroup_name = 'lr-sg-' + UUID
    secgroup = nova.security_groups.create(
        secgroup_name, '')
    logging.info("Creating security group '%s'", secgroup_name)
    nova.security_group_rules.create(
        secgroup.id,
        'tcp',
        1,
        65535,
        '0.0.0.0/0'
    )

    nova.security_group_rules.create(
        secgroup.id,
        'udp',
        1,
        65535,
        '0.0.0.0/0'
    )

    nova.security_group_rules.create(
        secgroup.id,
        'icmp',
        -1,
        -1,
        '0.0.0.0/0'
    )
    logging.info("Created, secgroup_id=%s", secgroup.id)
    return secgroup


def nova_create_lr_flavor():
    logging.info("Creating flavor for loadrunner")
    flavor = get_nova_client().flavors.create(
        'lr-flavor-lr-' + UUID, 4096, 2, 20);
    logging.info("Created flavor, name=%s", flavor.name)
    return flavor


def nova_create_agent_flavor():
    logging.info("Creating flavor for agent")
    flavor = get_nova_client().flavors.create(
        'lr-flavor-agent-' + UUID, 2048, 1, 20);
    logging.info("Created flavor, name=%s", flavor.name)
    return flavor


def nova_boot(image, flavor, keypair, secgroup):
    server_name = 'loadrunner-' + UUID
    logging.info("Booting server '%s', flavor_id=%s, image_id=%s, "
                 "keypair_id=%s, secgroup_id=%s", server_name,
                 flavor.id, image.id, keypair.id, secgroup.id)
    nova = get_nova_client()
    server = nova.servers.create(
        server_name, image, flavor,key_name=keypair.name,
        security_groups=[secgroup.name]
    )
    while server.status == 'BUILD':
        time.sleep(5)
        server = nova.servers.get(server.id)
        print '.'

    if server.status == 'ACTIVE':
        logging.info("Server created, id=%s", server.id)
        return server

    logging.error('Failed booting server, status=%s', server.status)
    return None


def prepare_file(template_name, file_name):
    with open(template_name) as t:
        templ = string.Template(t.read())
        text = templ.substitute(globals())
        newname = file_name + UUID
        with open(newname, 'w') as f:
            f.write(text)

    logging.info('Prepared %s', newname)
    return newname


def get_ssh_client(host, user, keypair):
    global __ssh_client
    if not __ssh_client:
        logging.info('Connecting via SSH to %s@%s', user, host)
        client = sshclient.SSHClient()
        client.load_system_host_keys()
        client.set_missing_host_key_policy(sshclient.AutoAddPolicy())
        retries_left = 10
        while retries_left:
            try:
                print '.'
                client.connect(host, username=user,
                               pkey=sshclient.rsakey.RSAKey.from_private_key(
                                   StringIO.StringIO(keypair.private_key)))
                __ssh_client = client
                logging.info('Connected')
                return __ssh_client
            except socket_error as e:
                logging.info('Connect error %s', e)
            time.sleep(10)
        logging.error('Connecting to %s@%s failed', user, host)
        exit(-1)
    return __ssh_client


def get_floating_ip():
    logging.info('Looking for available floating ip')
    nova = get_nova_client()
    floating_ips = nova.floating_ips.list()
    for ip in floating_ips:
        if not ip.instance_id and not ip.fixed_ip:
            logging.info('Found %s', ip.ip)
            return ip
    logging.error('No floating IP available')
    exit(-1)


def get_ip(server, nw_name):
    fip = get_floating_ip()
    server.add_floating_ip(fip)
    logging.info("Associated floating IP %s to server '%s'", fip.ip,
                 server.id)
    return fip.ip

    logging.info('Looking for network address')
    logging.info('Available networks %s', server.networks.keys())
    for network in server.networks[nw_name]:
        if re.match('\d+\.\d+\.\d+\.\d+', network):
            logging.info('Network address found, %s', network)
            return network

    logging.error('No network address found')
    exit(-1)
    return None


def prepare_network():
    global MANAGEMENT_NET_NAME
    global MANAGEMENT_NET_CIDR
    global MANAGEMENT_NET_ID

    neutron = get_neutron_client()

    if MANAGEMENT_NET_ID:
        network = neutron.show_network(MANAGEMENT_NET_ID)['network']
        MANAGEMENT_NET_NAME = network['name']
        if not MANAGEMENT_NET_CIDR:
            MANAGEMENT_NET_CIDR = neutron.show_subnet(
                network['subnets'][0]
            )['cidr']
    elif MANAGEMENT_NET_NAME:
        network = neutron.list_networks(
            name=MANAGEMENT_NET_NAME
        )['networks'][0]
        MANAGEMENT_NET_ID = network['id']
        if not MANAGEMENT_NET_CIDR:
            MANAGEMENT_NET_CIDR = neutron.show_subnet(
                network['subnets'][0]
            )['subnet']['cidr']
    else:
        logging.error('Neither MANAGEMENT_NET_ID nor MANAGEMENT_NET_NAME'
                      ' is defined')
        exit(-1)

    logging.info('MANAGEMENT_NET_ID={}'.format(MANAGEMENT_NET_ID))
    logging.info('MANAGEMENT_NET_NAME={}'.format(MANAGEMENT_NET_NAME))
    logging.info('MANAGEMENT_NET_CIDR={}'.format(MANAGEMENT_NET_CIDR))


def run():
    log_env()
    prepare_network()

    keypair = nova_keypair_add()
    secgroup = nova_create_secgroup()

    lr_flavor = nova_create_lr_flavor()
    agent_flavor = nova_create_agent_flavor()
    global AGENT_FLAVOR_ID
    AGENT_FLAVOR_ID = agent_flavor.id

    lr_image = glance_upload_image('loadrunner', LOADRUNNER_IMAGE_NAME)
    agent_image = glance_upload_image('agent', AGENT_IMAGE_NAME)
    global AGENT_IMAGE_ID
    AGENT_IMAGE_ID = agent_image.id

    server = nova_boot(
        lr_image,
        lr_flavor,
        keypair,
        secgroup
    )

    server_ip = get_ip(server, MANAGEMENT_NET_NAME)

    global OS_TOKEN
    OS_TOKEN = get_keystone_client().auth_token

    settings_py = prepare_file(SETTINGS_TEMPLATE, 'settings.py.')
    test_yml = prepare_file(TEST_TEMPLATE, 'test.yml.')

    ssh = get_ssh_client(server_ip, LOADRUNNER_USER, keypair)
    sftp = ssh.open_sftp()
    logging.info("Copying local '%s' to remote '%s'", settings_py,
                 SETTINGS_REMOTE_PATH)
    sftp.put(settings_py, SETTINGS_REMOTE_PATH)
    logging.info('Done')
    logging.info("Copying local '%s' to remote '%s'", test_yml,
                 TEST_REMOTE_PATH)
    sftp.put(test_yml, TEST_REMOTE_PATH)
    logging.info('Done')

    logging.info('Setting up SDN tool in loadrunner VM')
    stdin, stdout, stderr = ssh.exec_command(
        'cd /home/ubuntu/load_runner && python setup.py install --user')

    status = stdout.channel.recv_exit_status()

    logging.info('Exit status %d', status)

    fname = 'setup-stdout-{}.log'.format(UUID)
    logging.info('Saving stdout to %s', fname)
    with open(fname, 'w') as f:
        f.write(stdout.read())

    fname = 'setup-stderr-{}.log'.format(UUID)
    logging.info('Saving stderr to %s', fname)
    with open(fname, 'w') as f:
        f.write(stderr.read())

    logging.info('Running test')
    stdin, stdout, stderr = ssh.exec_command(
        'cd /home/ubuntu/load_runner/load_runner && python run.py -t'
        '{}'.format(TEST_NAME))

    status = stdout.channel.recv_exit_status()

    logging.info('Exit status %d', status)

    fname = 'run-pass-1-stdout-{}.log'.format(UUID)
    logging.info('Saving stdout to %s', fname)
    with open(fname, 'w') as f:
        f.write(stdout.read())

    fname = 'run-pass-1-stderr-{}.log'.format(UUID)
    logging.info('Saving stderr to %s', fname)
    with open(fname, 'w') as f:
        f.write(stderr.read())

    logging.info('Waiting 300 seconds before the second pass')
    time.sleep(300)

    logging.info('Running test, pass #2')
    stdin, stdout, stderr = ssh.exec_command(
        'cd /home/ubuntu/load_runner/load_runner && python run.py ',
        '{}'.format(TEST_NAME))

    status = stdout.channel.recv_exit_status()

    logging.info('Exit status %d', status)

    fname = 'run-pass-2-stdout-{}.log'.format(UUID)
    logging.info('Saving stdout to %s', fname)
    with open(fname, 'w') as f:
        f.write(stdout.read())

    fname = 'run-pass-2-stderr-{}.log'.format(UUID)
    logging.info('Saving stderr to %s', fname)
    with open(fname, 'w') as f:
        f.write(stderr.read())

    logging.info("Cleaning up")

    logging.info("Deleting server %s, id=%s",
                 server.name, server.id)
    server.delete()

    logging.info("Deleting agent image %s, id=%s",
                 agent_image.name, agent_image.id)
    get_glance_client().images.delete(agent_image.id)

    logging.info("Deleting agent flavor %s, id=%s",
                 agent_flavor.name, agent_flavor.id)
    agent_flavor.delete()

    logging.info("Deleting loadrunner image %s, id=%s",
                 lr_image.name, lr_image.id)
    get_glance_client().images.delete(lr_image.id)

    logging.info("Deleting loadrunner flavor %s, id=%s",
                 lr_flavor.name, lr_flavor.id)
    lr_flavor.delete()

    logging.info("Deleting keypair %s", keypair.id)
    keypair.delete()

    time.sleep(10) # wait while loadrunner server actually gets deleted

    logging.info("Deleting security group %s, id=%s",
                 secgroup.name, secgroup.id)
    secgroup.delete()

    logging.info("Done")


if __name__ == '__main__':
    run()

