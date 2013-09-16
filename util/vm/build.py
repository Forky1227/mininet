#!/usr/bin/python

"""
build.py: build a Mininet VM

Basic idea:

    prepare
    -> create base install image if it's missing
        - download iso if it's missing
        - install from iso onto image

    build
    -> create cow disk for new VM, based on base image
    -> boot it in qemu/kvm with text /serial console
    -> install Mininet

    test
    -> sudo mn --test pingall
    -> make test

    release
    -> shut down VM
    -> shrink-wrap VM
    -> upload to storage

"""

import os
from os import stat, path
from stat import ST_MODE, ST_SIZE
from os.path import abspath
from sys import exit, stdout, argv, modules
import re
from glob import glob
from subprocess import check_output, call, Popen
from tempfile import mkdtemp, NamedTemporaryFile
from time import time, strftime, localtime
import argparse
from distutils.spawn import find_executable
import inspect

pexpect = None  # For code check - imported dynamically

# boot can be slooooow!!!! need to debug/optimize somehow
TIMEOUT=600

# Some configuration options
# Possibly change this to use the parsed arguments instead!

LogToConsole = False        # VM output to console rather than log file
SaveQCOW2 = False           # Save QCOW2 image rather than deleting it
NoKVM = False               # Don't use kvm and use emulation instead
Branch = None               # Branch to update and check out before testing
Zip = False                  # Archive .ovf and .vmdk into a .zip file

VMImageDir = os.environ[ 'HOME' ] + '/vm-images'

Prompt = '\$ '              # Shell prompt that pexpect will wait for

isoURLs = {
    'precise32server':
    'http://mirrors.kernel.org/ubuntu-releases/12.04/'
    'ubuntu-12.04.3-server-i386.iso',
    'precise64server':
    'http://mirrors.kernel.org/ubuntu-releases/12.04/'
    'ubuntu-12.04.3-server-amd64.iso',
    'quantal32server':
    'http://mirrors.kernel.org/ubuntu-releases/12.10/'
    'ubuntu-12.10-server-i386.iso',
    'quantal64server':
    'http://mirrors.kernel.org/ubuntu-releases/12.10/'
    'ubuntu-12.10-server-amd64.iso',
    'raring32server':
    'http://mirrors.kernel.org/ubuntu-releases/13.04/'
    'ubuntu-13.04-server-i386.iso',
    'raring64server':
    'http://mirrors.kernel.org/ubuntu-releases/13.04/'
    'ubuntu-13.04-server-amd64.iso',
    'saucy32server':
    'http://mirrors.kernel.org/ubuntu-releases/13.10/'
    'ubuntu-13.10-server-i386.iso',
    'saucy64server':
    'http://mirrors.kernel.org/ubuntu-releases/13.10/'
    'ubuntu-13.10-server-amd64.iso',
}


def OSVersion( flavor ):
    "Return full OS version string for build flavor"
    urlbase = path.basename( isoURLs.get( flavor, 'unknown' ) )
    return path.splitext( urlbase )[ 0 ]


LogStartTime = time()
LogFile = None

def log( *args, **kwargs ):
    """Simple log function: log( message along with local and elapsed time
       cr: False/0 for no CR"""
    cr = kwargs.get( 'cr', True )
    elapsed = time() - LogStartTime
    clocktime = strftime( '%H:%M:%S', localtime() )
    msg = ' '.join( str( arg ) for arg in args )
    output = '%s [ %.3f ] %s' % ( clocktime, elapsed, msg )
    if cr:
        print output
    else:
        print output,
    # Optionally mirror to LogFile
    if type( LogFile ) is file:
        if cr:
            output += '\n'
        LogFile.write( output )
        LogFile.flush()


def run( cmd, **kwargs ):
    "Convenient interface to check_output"
    log( '-', cmd )
    cmd = cmd.split()
    arg0 = cmd[ 0 ]
    if not find_executable( arg0 ):
        raise Exception( 'Cannot find executable "%s";' % arg0 +
                         'you might try %s --depend' % argv[ 0 ] )
    return check_output( cmd, **kwargs )


def srun( cmd, **kwargs ):
    "Run + sudo"
    return run( 'sudo ' + cmd, **kwargs )


# BL: we should probably have a "checkDepend()" which
# checks to make sure all dependencies are satisfied!

def depend():
    "Install package dependencies"
    log( '* Installing package dependencies' )
    run( 'sudo apt-get -y update' )
    run( 'sudo apt-get install -y'
         ' kvm cloud-utils genisoimage qemu-kvm qemu-utils'
         ' e2fsprogs '
         ' landscape-client'
         ' python-setuptools mtools zip' )
    run( 'sudo easy_install pexpect' )


def popen( cmd ):
    "Convenient interface to popen"
    log( cmd )
    cmd = cmd.split()
    return Popen( cmd )


def remove( fname ):
    "Remove a file, ignoring errors"
    try:
        os.remove( fname )
    except OSError:
        pass


def findiso( flavor ):
    "Find iso, fetching it if it's not there already"
    url = isoURLs[ flavor ]
    name = path.basename( url )
    iso = path.join( VMImageDir, name )
    if not path.exists( iso ) or ( stat( iso )[ ST_MODE ] & 0777 != 0444 ):
        log( '* Retrieving', url )
        run( 'curl -C - -o %s %s' % ( iso, url ) )
        if 'ISO' not in run( 'file ' + iso ):
            os.remove( iso )
            raise Exception( 'findiso: could not download iso from ' + url )
        # Write-protect iso, signaling it is complete
        log( '* Write-protecting iso', iso)
        os.chmod( iso, 0444 )
    log( '* Using iso', iso )
    return iso


def attachNBD( cow, flags='' ):
    """Attempt to attach a COW disk image and return its nbd device
        flags: additional flags for qemu-nbd (e.g. -r for readonly)"""
    # qemu-nbd requires an absolute path
    cow = abspath( cow )
    log( '* Checking for unused /dev/nbdX device ' )
    for i in range ( 0, 63 ):
        nbd = '/dev/nbd%d' % i
        # Check whether someone's already messing with that device
        if call( [ 'pgrep', '-f', nbd ] ) == 0:
            continue
        srun( 'modprobe nbd max-part=64' )
        srun( 'qemu-nbd %s -c %s %s' % ( flags, nbd, cow ) )
        print
        return nbd
    raise Exception( "Error: could not find unused /dev/nbdX device" )


def detachNBD( nbd ):
    "Detatch an nbd device"
    srun( 'qemu-nbd -d ' + nbd )


def extractKernel( image, flavor, imageDir=VMImageDir ):
    "Extract kernel and initrd from base image"
    kernel = path.join( imageDir, flavor + '-vmlinuz' )
    initrd = path.join( imageDir, flavor + '-initrd' )
    if path.exists( kernel ) and ( stat( image )[ ST_MODE ] & 0777 ) == 0444:
        # If kernel is there, then initrd should also be there
        return kernel, initrd
    log( '* Extracting kernel to', kernel )
    nbd = attachNBD( image, flags='-r' )
    print srun( 'partx ' + nbd )
    # Assume kernel is in partition 1/boot/vmlinuz*generic for now
    part = nbd + 'p1'
    mnt = mkdtemp()
    srun( 'mount -o ro %s %s' % ( part, mnt  ) )
    kernsrc = glob( '%s/boot/vmlinuz*generic' % mnt )[ 0 ]
    initrdsrc = glob( '%s/boot/initrd*generic' % mnt )[ 0 ]
    srun( 'cp %s %s' % ( initrdsrc, initrd ) )
    srun( 'chmod 0444 ' + initrd )
    srun( 'cp %s %s' % ( kernsrc, kernel ) )
    srun( 'chmod 0444 ' + kernel )
    srun( 'umount ' + mnt )
    run( 'rmdir ' + mnt )
    detachNBD( nbd )
    return kernel, initrd


def findBaseImage( flavor, size='8G' ):
    "Return base VM image and kernel, creating them if needed"
    image = path.join( VMImageDir, flavor + '-base.qcow2' )
    if path.exists( image ):
        # Detect race condition with multiple builds
        perms = stat( image )[ ST_MODE ] & 0777
        if perms != 0444:
            raise Exception( 'Error - %s is writable ' % image +
                            '; are multiple builds running?' )
    else:
        # We create VMImageDir here since we are called first
        run( 'mkdir -p %s' % VMImageDir )
        iso = findiso( flavor )
        log( '* Creating image file', image )
        run( 'qemu-img create -f qcow2 %s %s' % ( image, size ) )
        installUbuntu( iso, image )
        # Write-protect image, also signaling it is complete
        log( '* Write-protecting image', image)
        os.chmod( image, 0444 )
    kernel, initrd = extractKernel( image, flavor )
    log( '* Using base image', image, 'and kernel', kernel )
    return image, kernel, initrd


# Kickstart and Preseed files for Ubuntu/Debian installer
#
# Comments: this is really clunky and painful. If Ubuntu
# gets their act together and supports kickstart a bit better
# then we can get rid of preseed and even use this as a
# Fedora installer as well.
#
# Another annoying thing about Ubuntu is that it can't just
# install a normal system from the iso - it has to download
# junk from the internet, making this house of cards even
# more precarious.

KickstartText ="""
#Generated by Kickstart Configurator
#platform=x86

#System language
lang en_US
#Language modules to install
langsupport en_US
#System keyboard
keyboard us
#System mouse
mouse
#System timezone
timezone America/Los_Angeles
#Root password
rootpw --disabled
#Initial user
user mininet --fullname "mininet" --password "mininet"
#Use text mode install
text
#Install OS instead of upgrade
install
#Use CDROM installation media
cdrom
#System bootloader configuration
bootloader --location=mbr
#Clear the Master Boot Record
zerombr yes
#Partition clearing information
clearpart --all --initlabel
#Automatic partitioning
autopart
#System authorization infomation
auth  --useshadow  --enablemd5
#Firewall configuration
firewall --disabled
#Do not configure the X Window System
skipx
"""

# Tell the Ubuntu/Debian installer to stop asking stupid questions

PreseedText = """
d-i mirror/country string manual
d-i mirror/http/hostname string mirrors.kernel.org
d-i mirror/http/directory string /ubuntu
d-i mirror/http/proxy string
d-i partman/confirm_write_new_label boolean true
d-i partman/choose_partition select finish
d-i partman/confirm boolean true
d-i partman/confirm_nooverwrite boolean true
d-i user-setup/allow-password-weak boolean true
d-i finish-install/reboot_in_progress note
d-i debian-installer/exit/poweroff boolean true
"""

def makeKickstartFloppy():
    "Create and return kickstart floppy, kickstart, preseed"
    kickstart = 'ks.cfg'
    with open( kickstart, 'w' ) as f:
        f.write( KickstartText )
    preseed = 'ks.preseed'
    with open( preseed, 'w' ) as f:
        f.write( PreseedText )
    # Create floppy and copy files to it
    floppy = 'ksfloppy.img'
    run( 'qemu-img create %s 1440k' % floppy )
    run( 'mkfs -t msdos ' + floppy )
    run( 'mcopy -i %s %s ::/' % ( floppy, kickstart ) )
    run( 'mcopy -i %s %s ::/' % ( floppy, preseed ) )
    return floppy, kickstart, preseed


def archFor( filepath ):
    "Guess architecture for file path"
    name = path.basename( filepath )
    if '64' in name:
        arch = 'x86_64'
    elif 'i386' in name or '32' in name:
        arch = 'i386'
    else:
        log( "Error: can't discern CPU for file name", name )
        exit( 1 )
    return arch


def installUbuntu( iso, image, logfilename='install.log' ):
    "Install Ubuntu from iso onto image"
    kvm = 'qemu-system-' + archFor( iso )
    floppy, kickstart, preseed = makeKickstartFloppy()
    # Mount iso so we can use its kernel
    mnt = mkdtemp()
    srun( 'mount %s %s' % ( iso, mnt ) )
    kernel = path.join( mnt, 'install/vmlinuz' )
    initrd = path.join( mnt, 'install/initrd.gz' )
    if NoKVM:
        accel = 'tcg'
    else:
        accel = 'kvm'
    cmd = [ 'sudo', kvm,
           '-machine', 'accel=%s' % accel,
           '-nographic',
           '-netdev', 'user,id=mnbuild',
           '-device', 'virtio-net,netdev=mnbuild',
           '-m', '1024',
           '-k', 'en-us',
           '-fda', floppy,
           '-drive', 'file=%s,if=virtio' % image,
           '-cdrom', iso,
           '-kernel', kernel,
           '-initrd', initrd,
           '-append',
           ' ks=floppy:/' + kickstart +
           ' preseed/file=floppy://' + preseed +
           ' console=ttyS0' ]
    ubuntuStart = time()
    log( '* INSTALLING UBUNTU FROM', iso, 'ONTO', image )
    log( ' '.join( cmd ) )
    log( '* logging to', abspath( logfilename ) )
    params = {}
    if not LogToConsole:
        logfile = open( logfilename, 'w' )
        params = { 'stdout': logfile, 'stderr': logfile }
    vm = Popen( cmd, **params )
    log( '* Waiting for installation to complete')
    vm.wait()
    if not LogToConsole:
        logfile.close()
    elapsed = time() - ubuntuStart
    # Unmount iso and clean up
    srun( 'umount ' + mnt )
    run( 'rmdir ' + mnt )
    if vm.returncode != 0:
        raise Exception( 'Ubuntu installation returned error %d' %
                          vm.returncode )
    log( '* UBUNTU INSTALLATION COMPLETED FOR', image )
    log( '* Ubuntu installation completed in %.2f seconds' % elapsed )


def boot( cow, kernel, initrd, logfile ):
    """Boot qemu/kvm with a COW disk and local/user data store
       cow: COW disk path
       kernel: kernel path
       logfile: log file for pexpect object
       returns: pexpect object to qemu process"""
    # pexpect might not be installed until after depend() is called
    global pexpect
    import pexpect
    arch = archFor( kernel )
    if NoKVM:
        accel = 'tcg'
    else:
        accel = 'kvm'
    cmd = [ 'sudo', 'qemu-system-' + arch,
            '-machine accel=%s' % accel,
            '-nographic',
            '-netdev user,id=mnbuild',
            '-device virtio-net,netdev=mnbuild',
            '-m 1024',
            '-k en-us',
            '-kernel', kernel,
            '-initrd', initrd,
            '-drive file=%s,if=virtio' % cow,
            '-append "root=/dev/vda1 init=/sbin/init console=ttyS0" ' ]
    cmd = ' '.join( cmd )
    log( '* BOOTING VM FROM', cow )
    log( cmd )
    vm = pexpect.spawn( cmd, timeout=TIMEOUT, logfile=logfile )
    return vm


def login( vm ):
    "Log in to vm (pexpect object)"
    log( '* Waiting for login prompt' )
    vm.expect( 'login: ' )
    log( '* Logging in' )
    vm.sendline( 'mininet' )
    log( '* Waiting for password prompt' )
    vm.expect( 'Password: ' )
    log( '* Sending password' )
    vm.sendline( 'mininet' )
    log( '* Waiting for login...' )


def sanityTest( vm ):
    "Run Mininet sanity test (pingall) in vm"
    vm.sendline( 'sudo mn --test pingall' )
    if vm.expect( [ ' 0% dropped', pexpect.TIMEOUT ], timeout=45 ) == 0:
        log( '* Sanity check OK' )
    else:
        log( '* Sanity check FAILED' )
        log( '* Sanity check output:' )
        log( vm.before )


def coreTest( vm, prompt=Prompt ):
    "Run core tests (make test) in VM"
    log( '* Making sure cgroups are mounted' )
    vm.sendline( 'sudo service cgroup-lite restart' )
    vm.expect( prompt )
    vm.sendline( 'sudo cgroups-mount' )
    vm.expect( prompt )
    log( '* Running make test' )
    vm.sendline( 'cd ~/mininet; sudo make test' )
    # We should change "make test" to report the number of
    # successful and failed tests. For now, we have to
    # know the time for each test, which means that this
    # script will have to change as we add more tests.
    for test in range( 0, 2 ):
        if vm.expect( [ 'OK', 'FAILED', pexpect.TIMEOUT ], timeout=180 ) == 0:
            log( '* Test', test, 'OK' )
        else:
            log( '* Test', test, 'FAILED' )
            log( '* Test', test, 'output:' )
            log( vm.before )

def examplesquickTest( vm, prompt=Prompt ):
    "Quick test of mininet examples"
    vm.sendline( 'sudo apt-get install python-pexpect' )
    vm.expect( prompt )
    vm.sendline( 'sudo python ~/mininet/examples/test/runner.py -v -quick' )


def examplesfullTest( vm, prompt=Prompt ):
    "Full (slow) test of mininet examples"
    vm.sendline( 'sudo apt-get install python-pexpect' )
    vm.expect( prompt )
    vm.sendline( 'sudo python ~/mininet/examples/test/runner.py -v' )


def checkOutBranch( vm, branch, prompt=Prompt ):
    vm.sendline( 'cd ~/mininet; git fetch; git pull --rebase; git checkout '
                 + branch )
    vm.expect( prompt )
    vm.sendline( 'sudo make install' )


def interact( vm, prompt=Prompt ):
    "Interact with vm, which is a pexpect object"
    login( vm )
    log( '* Waiting for login...' )
    vm.expect( prompt )
    log( '* Sending hostname command' )
    vm.sendline( 'hostname' )
    log( '* Waiting for output' )
    vm.expect( prompt )
    log( '* Fetching Mininet VM install script' )
    vm.sendline( 'wget '
                 'https://raw.github.com/mininet/mininet/master/util/vm/'
                 'install-mininet-vm.sh' )
    vm.expect( prompt )
    log( '* Running VM install script' )
    vm.sendline( 'bash install-mininet-vm.sh' )
    vm.expect ( 'password for mininet: ' )
    vm.sendline( 'mininet' )
    log( '* Waiting for script to complete... ' )
    # Gigantic timeout for now ;-(
    vm.expect( 'Done preparing Mininet', timeout=3600 )
    log( '* Completed successfully' )
    vm.expect( prompt )
    version = getMininetVersion( vm )
    vm.expect( prompt )
    log( '* Mininet version: ', version )
    log( '* Testing Mininet' )
    runTests( vm )
    log( '* Shutting down' )
    vm.sendline( 'sync; sudo shutdown -h now' )
    log( '* Waiting for EOF/shutdown' )
    vm.read()
    log( '* Interaction complete' )
    return version


def cleanup():
    "Clean up leftover qemu-nbd processes and other junk"
    call( [ 'sudo', 'pkill', '-9', 'qemu-nbd' ] )


def convert( cow, basename ):
    """Convert a qcow2 disk to a vmdk and put it a new directory
       basename: base name for output vmdk file"""
    vmdk = basename + '.vmdk'
    log( '* Converting qcow2 to vmdk' )
    run( 'qemu-img convert -f qcow2 -O vmdk %s %s' % ( cow, vmdk ) )
    return vmdk


# Template for OVF - a very verbose format!
# In the best of all possible worlds, we might use an XML
# library to generate this, but a template is easier and
# possibly more concise!
# Warning: XML file cannot begin with a newline!

OVFTemplate = """<?xml version="1.0"?>
<Envelope ovf:version="1.0" xml:lang="en-US"
    xmlns="http://schemas.dmtf.org/ovf/envelope/1"
    xmlns:ovf="http://schemas.dmtf.org/ovf/envelope/1"
    xmlns:rasd="http://schemas.dmtf.org/wbem/wscim/1/cim-schema/2/CIM_ResourceAllocationSettingData"
    xmlns:vssd="http://schemas.dmtf.org/wbem/wscim/1/cim-schema/2/CIM_VirtualSystemSettingData"
    xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
<References>
<File ovf:href="%s" ovf:id="file1" ovf:size="%d"/>
</References>
<DiskSection>
<Info>Virtual disk information</Info>
<Disk ovf:capacity="%d" ovf:capacityAllocationUnits="byte" 
    ovf:diskId="vmdisk1" ovf:fileRef="file1" 
    ovf:format="http://www.vmware.com/interfaces/specifications/vmdk.html"/>
</DiskSection>
<NetworkSection>
<Info>The list of logical networks</Info>
<Network ovf:name="nat">
<Description>The nat  network</Description>
</Network>
</NetworkSection>
<VirtualSystem ovf:id="Mininet-VM">
<Info>A Mininet Virtual Machine (%s)</Info>
<Name>mininet-vm</Name>
<VirtualHardwareSection>
<Info>Virtual hardware requirements</Info>
<Item>
<rasd:AllocationUnits>hertz * 10^6</rasd:AllocationUnits>
<rasd:Description>Number of Virtual CPUs</rasd:Description>
<rasd:ElementName>1 virtual CPU(s)</rasd:ElementName>
<rasd:InstanceID>1</rasd:InstanceID>
<rasd:ResourceType>3</rasd:ResourceType>
<rasd:VirtualQuantity>1</rasd:VirtualQuantity>
</Item>
<Item>
<rasd:AllocationUnits>byte * 2^20</rasd:AllocationUnits>
<rasd:Description>Memory Size</rasd:Description>
<rasd:ElementName>%dMB of memory</rasd:ElementName>
<rasd:InstanceID>2</rasd:InstanceID>
<rasd:ResourceType>4</rasd:ResourceType>
<rasd:VirtualQuantity>%d</rasd:VirtualQuantity>
</Item>
<Item>
<rasd:Address>0</rasd:Address>
<rasd:Caption>scsiController0</rasd:Caption>
<rasd:Description>SCSI Controller</rasd:Description>
<rasd:ElementName>scsiController0</rasd:ElementName>
<rasd:InstanceID>4</rasd:InstanceID>
<rasd:ResourceSubType>lsilogic</rasd:ResourceSubType>
<rasd:ResourceType>6</rasd:ResourceType>
</Item>
<Item>
<rasd:AddressOnParent>0</rasd:AddressOnParent>
<rasd:ElementName>disk1</rasd:ElementName>
<rasd:HostResource>ovf:/disk/vmdisk1</rasd:HostResource>
<rasd:InstanceID>11</rasd:InstanceID>
<rasd:Parent>4</rasd:Parent>
<rasd:ResourceType>17</rasd:ResourceType>
</Item>
<Item>
<rasd:AddressOnParent>2</rasd:AddressOnParent>
<rasd:AutomaticAllocation>true</rasd:AutomaticAllocation>
<rasd:Connection>nat</rasd:Connection>
<rasd:Description>E1000 ethernet adapter on nat</rasd:Description>
<rasd:ElementName>ethernet0</rasd:ElementName>
<rasd:InstanceID>12</rasd:InstanceID>
<rasd:ResourceSubType>E1000</rasd:ResourceSubType>
<rasd:ResourceType>10</rasd:ResourceType>
</Item>
<Item>
<rasd:Address>0</rasd:Address>
<rasd:Caption>usb</rasd:Caption>
<rasd:Description>USB Controller</rasd:Description>
<rasd:ElementName>usb</rasd:ElementName>
<rasd:InstanceID>9</rasd:InstanceID>
<rasd:ResourceType>23</rasd:ResourceType>
</Item>
</VirtualHardwareSection>
</VirtualSystem>
</Envelope>
"""


def generateOVF( name, diskname, disksize, mem=1024 ):
    """Generate (and return) OVF file "name.ovf"
       name: root name of OVF file to generate
       diskname: name of disk file
       disksize: size of virtual disk in bytes
       mem: VM memory size in MB"""
    ovf = name + '.ovf'
    filesize = stat( diskname )[ ST_SIZE ]
    # OVFTemplate uses the memory size twice in a row
    xmltext = OVFTemplate % ( diskname, filesize, disksize, name, mem, mem )
    with open( ovf, 'w+' ) as f:
        f.write( xmltext )
    return ovf


def qcow2size( qcow2 ):
    "Return virtual disk size (in bytes) of qcow2 image"
    output = check_output( [ 'file', qcow2 ] )
    assert 'QCOW' in output
    bytes = int( re.findall( '(\d+) bytes', output )[ 0 ] )
    return bytes


def build( flavor='raring32server' ):
    "Build a Mininet VM; return vmdk and vdisk size"
    global LogFile, Zip
    start = time()
    date = strftime( '%y%m%d-%H-%M-%S', localtime())
    dir = 'mn-%s-%s' % ( flavor, date )
    try:
        os.mkdir( dir )
    except:
        raise Exception( "Failed to create build directory %s" % dir )
    os.chdir( dir )
    LogFile = open( 'build.log', 'w' )
    log( '* Logging to', abspath( LogFile.name ) )
    log( '* Created working directory', dir )
    image, kernel, initrd = findBaseImage( flavor )
    basename = 'mininet-' + flavor
    volume = basename + '.qcow2'
    run( 'qemu-img create -f qcow2 -b %s %s' % ( image, volume ) )
    log( '* VM image for', flavor, 'created as', volume )
    if LogToConsole:
        logfile = stdout
    else:
        logfile = open( flavor + '.log', 'w+' )
    log( '* Logging results to', abspath( logfile.name ) )
    vm = boot( volume, kernel, initrd, logfile )
    version = interact( vm )
    size = qcow2size( volume )
    vmdk = convert( volume, basename='mininet-vm' )
    if not SaveQCOW2:
        log( '* Removing qcow2 volume', volume )
        os.remove( volume )
    log( '* Converted VM image stored as', abspath( vmdk ) )
    ovfname = 'mininet-%s-%s' % ( version, OSVersion( flavor ) )
    ovf = generateOVF( diskname=vmdk, disksize=size, name=ovfname )
    log( '* Generated OVF descriptor file', ovf )
    if Zip:
        log( '* Generating .zip file' )
        run( 'zip %s-ovf.zip %s %s' % ( ovfname, ovf, vmdk ) )
    end = time()
    elapsed = end - start
    log( '* Results logged to', abspath( logfile.name ) )
    log( '* Completed in %.2f seconds' % elapsed )
    log( '* %s VM build DONE!!!!! :D' % flavor )
    os.chdir( '..' )


def runTests( vm, tests=None, prompt=Prompt ):
    "Run tests (list) in vm (pexpect object)"
    if not tests:
        tests = [ 'sanity', 'core' ]
    testfns = testDict()
    for test in tests:
        if test not in testfns:
            raise Exception( 'Unknown test: ' + test )
        log( '* Running test', test )
        fn = testfns[ test ]
        fn( vm )
        vm.expect( prompt )


def getMininetVersion( vm ):
    "Run mn to find Mininet version in VM"
    vm.sendline( '~/mininet/bin/mn --version' )
    # Eat command line echo, then read output line
    vm.readline()
    version = vm.readline().strip()
    return version


def bootAndRunTests( image, tests=None ):
    """Boot and test VM
       tests: list of tests (default: sanity, core)"""
    bootTestStart = time()
    basename = path.basename( image )
    image = abspath( image )
    tmpdir = mkdtemp( prefix='test-' + basename )
    log( '* Using tmpdir', tmpdir )
    cow = path.join( tmpdir, basename + '.qcow2' )
    log( '* Creating COW disk', cow )
    run( 'qemu-img create -f qcow2 -b %s %s' % ( image, cow ) )
    log( '* Extracting kernel and initrd' )
    kernel, initrd = extractKernel( image, flavor=basename, imageDir=tmpdir )
    if LogToConsole:
        logfile = stdout
    else:
        logfile = NamedTemporaryFile( prefix=basename,
                                      suffix='.testlog', delete=False )
    log( '* Logging VM output to', logfile.name )
    vm = boot( cow=cow, kernel=kernel, initrd=initrd, logfile=logfile )
    prompt = '\$ '
    login( vm )
    log( '* Waiting for VM boot and login' )
    vm.expect( prompt )
    if Branch:
        checkOutBranch( vm, branch=Branch )
        vm.expect( prompt )
    vm.expect( prompt )
    log( '* Running tests' )
    runTests( vm, tests=tests )
    # runTests eats its last prompt, but maybe it shouldn't...
    log( '* Shutting down' )
    vm.sendline( 'sudo shutdown -h now ' )
    log( '* Waiting for shutdown' )
    vm.wait()
    log( '* Removing temporary dir', tmpdir )
    srun( 'rm -rf ' + tmpdir )
    elapsed = time() - bootTestStart
    log( '* Boot and test completed in %.2f seconds' % elapsed )


def buildFlavorString():
    "Return string listing valid build flavors"
    return 'valid build flavors: ( %s )' % ' '.join( sorted( isoURLs ) )


def testDict():
    "Return dict of tests in this module"
    suffix = 'Test'
    trim = len( suffix )
    fdict = dict( [ ( fname[ : -trim ], f ) for fname, f in
                    inspect.getmembers( modules[ __name__ ],
                                    inspect.isfunction )
                  if fname.endswith( suffix ) ] )
    return fdict


def testString():
    "Return string listing valid tests"
    return 'valid tests: ( %s )' % ' '.join( testDict().keys() )


def parseArgs():
    "Parse command line arguments and run"
    global LogToConsole, NoKVM, Branch, Zip
    parser = argparse.ArgumentParser( description='Mininet VM build script',
                                      epilog=buildFlavorString() + ' ' +
                                      testString() )
    parser.add_argument( '-v', '--verbose', action='store_true',
                        help='send VM output to console rather than log file' )
    parser.add_argument( '-d', '--depend', action='store_true',
                         help='install dependencies for this script' )
    parser.add_argument( '-l', '--list', action='store_true',
                         help='list valid build flavors and tests' )
    parser.add_argument( '-c', '--clean', action='store_true',
                         help='clean up leftover build junk (e.g. qemu-nbd)' )
    parser.add_argument( '-q', '--qcow2', action='store_true',
                         help='save qcow2 image rather than deleting it' )
    parser.add_argument( '-n', '--nokvm', action='store_true',
                         help="Don't use kvm - use tcg emulation instead" )
    parser.add_argument( '-i', '--image', metavar='image', default=[],
                         action='append',
                         help='Boot and test an existing VM image' )
    parser.add_argument( '-t', '--test', metavar='test', default=[],
                        action='append',
                        help='specify a test to run' )
    parser.add_argument( '-b', '--branch', metavar='branch',
                         help='For an existing VM image, check out and install'
                         ' this branch before testing' )
    parser.add_argument( 'flavor', nargs='*',
                         help='VM flavor(s) to build (e.g. raring32server)' )
    parser.add_argument( '-z', '--zip', action='store_true',
                        help='archive .ovf and .vmdk into .zip file' )
    args = parser.parse_args()
    if args.depend:
        depend()
    if args.list:
        print buildFlavorString()
    if args.clean:
        cleanup()
    if args.verbose:
        LogToConsole = True
    if args.nokvm:
        NoKVM = True
    if args.branch:
        Branch = args.branch
    if args.zip:
        Zip = True
    for flavor in args.flavor:
        if flavor not in isoURLs:
            print "Unknown build flavor:", flavor
            print buildFlavorString()
            break
        # try:
        build( flavor )
        # except Exception as e:
        # log( '* BUILD FAILED with exception: ', e )
        # exit( 1 )
    for image in args.image:
        bootAndRunTests( image, tests=args.test )
    if not ( args.depend or args.list or args.clean or args.flavor
             or args.image ):
        parser.print_help()


if __name__ == '__main__':
    parseArgs()
