import boto.ec2
import boto.utils
import time
import sys
import socket
import uuid
from boto.exception import EC2ResponseError
from boto.ec2.blockdevicemapping import BlockDeviceType
from boto.ec2.blockdevicemapping import BlockDeviceMapping
import json
import os


def telnet_connection(host, port=22):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.connect_ex((host, port))
        return True
    except socket.error, msg:
        print msg.message
        return False


def get_ami(conn, ami_id):
    ''' Gets a single AMI object from original account '''
    attempts = 0
    max_attempts = 5
    while(attempts < max_attempts):
        try:
            attempts += 1
            images = conn.get_all_images(ami_id)
        except boto.exception.EC2ResponseError:
            msg = "Could not find AMI {} in region {}".format(ami_id, conn.region.name)
            if attempts < max_attempts:
                # The API call to initiate an AMI copy is not blocking, so the
                # copied AMI may not be available right away
                print(msg + ' so waiting 5 seconds and retrying')
                time.sleep(5)
            else:
                raise Exception(msg)

    print "Found AMIs: {}".format(images)
    if len(images) == 0:
        msg = "AMI not found"
        raise Exception(msg)
    elif len(images) > 1:
        msg = "Somehow more than 1 AMI was detected - this is a weird error"
        raise Exception(msg)

    return images[0]


def wait_for_ami_to_be_available(conn, ami_id):
    ''' Blocking wait until the AMI is available '''
    ami = get_ami(conn, ami_id)
    print('AMI details: %s', vars(ami))

    while ami.state != 'available':
        print "{} in {} not available, waiting...".format(ami_id, conn.region.name)
        time.sleep(5)
        ami = get_ami(conn, ami_id)

        if ami.state == 'failed':
            msg = "AMI {} is in a failed state and will never be available".format(ami_id)
            raise Exception(msg)

    return ami


def launch_instance(conn, ami_id):
    # EC2_KEY_HANDLE = 'SRF'
    # SECGROUP_HANDLE = ''
    print 'Launch new instance from {} ...'.format(ami_id)
    reservations = conn.run_instances(image_id=ami_id, instance_type='t2.micro',
                                      # key_name=EC2_KEY_HANDLE
                                      # security_groups=[SECGROUP_HANDLE]
                                      # subnet_id='subnet-907c7ae6',
                                      # security_group_ids=SECGROUP_HANDLE
                                      )
    instance = reservations.instances[0]
    # Check up on its status every so often
    status = instance.update()
    while status == 'pending':
        time.sleep(10)
        status = instance.update()

    if status == 'running':
        while not telnet_connection(instance.ip_address):
            time.sleep(10)
        new_tags = {"Name": "test_copy_ami", "Owner": "Uri.Nahshon"}
        instance.add_tags(new_tags)

    else:
        print('Instance status: ' + status)

    return instance


def connect_to_account(region, account_number, aws_access_key_id='', aws_secret_access_key=''):
    print 'Connect to account {}'.format(account_number)
    metadata = boto.utils.get_instance_metadata(timeout=2, num_retries=2)
    if 'iam' in metadata:
        current_account = metadata['iam']['info']['InstanceProfileArn'].split(':')[4]
        if current_account == account_number:
            print 'Try connect by IAMRole'
            return boto.ec2.connect_to_region(region)

    if aws_access_key_id and aws_secret_access_key:
        return boto.ec2.connect_to_region(region, aws_access_key_id=aws_access_key_id,
                                          aws_secret_access_key=aws_secret_access_key)
    elif os.path.exists('./AMI_users.json'):
        users_path = './AMI_users.json'
    else:
        users_path = '/etc/copyAMI/AMI_users.json'

    with open(users_path) as data_file:
        data = json.load(data_file)
        if account_number in data:
            aws_access_key_id = data[account_number]['aws_access_key_id']
            aws_secret_access_key = data[account_number]['aws_secret_access_key']
            return boto.ec2.connect_to_region(region, aws_access_key_id=aws_access_key_id,
                                              aws_secret_access_key=aws_secret_access_key)


def share_ami(source_image, copy_to_account):
    # bdm = source_image.block_device_mapping[source_image.root_device_name]
    print 'Set {} permission to account {}'.format(source_image.id, copy_to_account)
    result = source_image.set_launch_permissions(user_ids=copy_to_account)
    print 'Share AMI succeeded: {}'.format(result)


def share_snapshots(conn, source_image, copy_to_account):
    for item in source_image.block_device_mapping:
        snapshot_id = source_image.block_device_mapping[item].snapshot_id
        source_snapshot = conn.get_all_snapshots(snapshot_ids=snapshot_id)[0]
        source_snapshot.share(user_ids=[copy_to_account])


def copy_snapshot(connection, source_region, snapshot_id):
    """Copy a snapshot. Used to copy the snapshot separately from the AMI."""
    try:
        source_snapshot = connection.get_all_snapshots(snapshot_ids=snapshot_id)[0]
    except EC2ResponseError as exc:
        raise Exception('Getting the snapshot of the source AMI failed: %s', exc.error_message)

    try:
        target_snapshot_id = connection.copy_snapshot(
            source_region=source_region,
            source_snapshot_id=source_snapshot.id,
            description=source_snapshot.description)
    except EC2ResponseError as exc:
        raise Exception('Copying the snapshot of the source AMI failed: %s', exc.error_message)

    # wait until copying the snapshot has been finished
    while connection.get_all_snapshots(snapshot_ids=target_snapshot_id)[0].status == 'pending':
        print('Waiting for completion of the snapshot copy.')
        time.sleep(5)

    if connection.get_all_snapshots(snapshot_ids=target_snapshot_id)[0].status == 'error':
        raise Exception('Copying the snapshot of the source AMI failed: The new snapshot ' +
                        '(%s) is broken.', target_snapshot_id)

    return target_snapshot_id


def create_image(connection, source_image, block_device_map):
    """Create a new AMI out of the copied snapshot and the pre-defined block device map."""
    try:
        target_image_id = connection.register_image(
            name=source_image.name,
            architecture=source_image.architecture,
            kernel_id=source_image.kernel_id,
            ramdisk_id=source_image.ramdisk_id,
            root_device_name=source_image.root_device_name,
            block_device_map=block_device_map,
            virtualization_type=source_image.virtualization_type,
        )
    except EC2ResponseError as exc:
        raise Exception('The creation of the copied AMI failed: %s', exc.error_message)

    while connection.get_all_images(image_ids=target_image_id)[0].state == 'pending':
        print('Waiting for completion of the AMI {} creation.'.format(target_image_id))
        time.sleep(10)

    if connection.get_all_images(image_ids=target_image_id)[0].state == 'failed':
        raise Exception('The creation of the copied AMI failed. The new AMI (%s) is broken.', target_image_id)

    return target_image_id


def build_block_device_map(source_image, target_snapshot_id, target_snapshot_size):
    """Create a block device map which is used for the copied AMI.
    The created block device map contains a root volumes with 10GB of storage
    on general purpose SSD (gp2) as well as up to four ephemeral volumes.
    Storage volume as well as number of ephemeral volumes can be changed when
    launching an instance out of the resulting AMI.
    """
    root_device_name = source_image.root_device_name

    del_root_volume = source_image.block_device_mapping[root_device_name].delete_on_termination

    block_device_map = BlockDeviceMapping()
    block_device_map[root_device_name] = BlockDeviceType(snapshot_id=target_snapshot_id,
                                                         size=target_snapshot_size,
                                                         volume_type='gp2',
                                                         delete_on_termination=del_root_volume)

    for i in range(0, 4):
        device_name = '/dev/sd%s' % chr(98 + i)
        block_device_map[device_name] = BlockDeviceType(ephemeral_name='ephemeral%i' % i)

    return block_device_map


def copy_snapshots_by_ami(conn, source_image, region):
    block_device_map = BlockDeviceMapping()

    for item in source_image.block_device_mapping:
        source_snapshot_id = source_image.block_device_mapping[item].snapshot_id
        target_snapshot_size = source_image.block_device_mapping[item].size
        delete_on_termination = source_image.block_device_mapping[item].delete_on_termination
        volume_type = source_image.block_device_mapping[item].volume_type
        target_snapshot_id = copy_snapshot(conn, region, source_snapshot_id)
        print 'New snapshot created: {}'.format(target_snapshot_id)
        device_name = str(item)
        block_device_map[device_name] = BlockDeviceType(snapshot_id=target_snapshot_id, size=target_snapshot_size,
                                                        volume_type=volume_type, delete_on_termination=delete_on_termination)
    return block_device_map


def get_block_device_map(source_image):
    block_device_map = BlockDeviceMapping()

    for item in source_image.block_device_mapping:
        source_snapshot_id = source_image.block_device_mapping[item].snapshot_id

        target_snapshot_size = source_image.block_device_mapping[item].size
        delete_on_termination = source_image.block_device_mapping[item].delete_on_termination
        volume_type = source_image.block_device_mapping[item].volume_type
        device_name = str(item)
        block_device_map[device_name] = BlockDeviceType(snapshot_id=source_snapshot_id, size=target_snapshot_size,
                                                        volume_type=volume_type, delete_on_termination=delete_on_termination)
    return block_device_map


def wait_till_ami_copleted(conn, ami_id):
    while conn.get_all_images(image_ids=ami_id)[0].state == 'pending':
        print('Waiting for completion of the AMI creation {}.'.format(ami_id))
        time.sleep(10)

    if conn.get_all_images(image_ids=ami_id)[0].state == 'failed':
        raise Exception('The creation of the copied AMI failed. The new AMI (%s) is broken.', ami_id)


def create_ami_from_instance(conn, source_image):
    temp_instance = launch_instance(conn, source_image.id)
    try:
        ami_id_from_instance = conn.create_image(temp_instance.id, 'Copy of ' + source_image.name)
        time.sleep(60)
        wait_till_ami_copleted(conn, ami_id_from_instance)
        print 'New {} created from {}'.format(ami_id_from_instance, ami_id)
        # conn.terminate_instances(instance_ids=[temp_instance.id])
        temp_instance.terminate()
        print 'Terminate temp instance {}'.format(temp_instance.id)
        return ami_id_from_instance
    except Exception as ex:
        print 'Terminate temp instance {}'.format(temp_instance.id)
        temp_instance.terminate()
        raise ex


if __name__ == '__main__':
    from_access_key = ''
    from_secret_key = ''
    to_access_key = ''
    to_secret_key = ''
    ami_id = ''
    to_account = ''
    from_account = ''
    region = 'us-west-2'
    from_region = ''
    to_region = ''
    platform = 'linux'
    print sys.argv
    for arg in sys.argv:
        if "ami_id=" in arg:
            ami_id = arg.split("ami_id=")[1]
        elif "to_account=" in arg:
            to_account = arg.split("to_account=")[1]
        elif "from_account=" in arg:
            from_account = arg.split("from_account=")[1]
        elif "ami_region=" in arg:
            ami_region = arg.split("ami_region=")[1]
        elif "from_access_key=" in arg:
            from_access_key = arg.split("from_access_key=")[1]
        elif "from_secret_key=" in arg:
            from_secret_key = arg.split("from_secret_key=")[1]
        elif "to_access_key=" in arg:
            to_access_key = arg.split("to_access_key=")[1]
        elif "to_secret_key=" in arg:
            to_secret_key = arg.split("to_secret_key=")[1]
        elif arg.startswith('region='):
            region = arg.split("region=")[1]
        elif arg.startswith('from_region='):
            from_region = arg.split("from_region=")[1]
        elif arg.startswith('to_region='):
            to_region = arg.split("to_region=")[1]
        elif arg.startswith('platform='):
            platform = arg.split("platform=")[1]

    sys.stdout.flush()
    print 'Going to copy: {} from account {} {} to {} {}.'.format(ami_id, from_account, from_region, to_account, to_region)
    uuid = uuid.uuid1()
    if not from_region and region:
        from_region = region
        to_region = region
    if from_region:
        region = from_region
    conn = connect_to_account(from_region, from_account, from_access_key, from_secret_key)
    print conn
    source_image = get_ami(conn, ami_id)
    try:
        if from_account != "" and from_account != source_image.owner_id:
            raise Exception('Account {} cannot share, only the owner {}.'.format(from_account, source_image.owner_id))
        share_ami(source_image, to_account)
        share_snapshots(conn, source_image, to_account)
    except Exception:
        # block_device_map = get_block_device_map(source_image)
        # new_ami_id = create_image(conn, source_image, block_device_map)
        new_ami_id = create_ami_from_instance(conn, source_image)
        source_image = get_ami(conn, new_ami_id)
        share_ami(source_image, to_account)
        share_snapshots(conn, source_image, to_account)
        ami_id = new_ami_id

    source_image.add_tags({'uuid': uuid})

    if from_region and to_region and from_region != to_region:
        conn = connect_to_account(to_region, from_account, from_access_key, from_secret_key)
        print 'Copy {} from {} to {}, please wait...'.format(ami_id, from_region, to_region)
        new_ami = conn.copy_image(from_region, ami_id, name=source_image.name)
        time.sleep(90)
        wait_till_ami_copleted(conn, new_ami.image_id)
        print 'Copy completed from {} to {} new AMI ID {}.'.format(from_region, to_region, new_ami.image_id)
        new_ami = get_ami(conn, new_ami.image_id)
        if from_account == to_account:
            print 'Successfully completed'
            exit(0)
        share_ami(new_ami, to_account)
        share_snapshots(conn, new_ami, to_account)
        ami_id = new_ami.id

    # Connect to second account
    if not to_access_key and not to_secret_key:
        conn = connect_to_account(to_region, to_account, to_access_key, to_secret_key)
    else:
        conn = connect_to_account(to_region, to_account, to_access_key, to_secret_key)
    source_image = conn.get_image(ami_id)
    if source_image is None:
        raise Exception("Get image {} return None.".format(ami_id))
    source_image.add_tags({'Name': 'CopyAMI', 'uuid': uuid})
    block_device_map = copy_snapshots_by_ami(conn, source_image, to_region)
    new_ami_id = create_image(conn, source_image, block_device_map)
    print "New AMI created {}: ".format(new_ami_id)
    new_image = wait_for_ami_to_be_available(conn, new_ami_id)
    print '{} is available'.format(new_ami_id)
    new_image.add_tags(source_image.tags)
    new_image.add_tags({'Name': 'CopyAMI', 'uuid': uuid})
    print 'Tags added uuid:{} & {}.'.format(uuid, source_image.tags)
    print 'New AMI complete {}.'.format(new_ami_id)
