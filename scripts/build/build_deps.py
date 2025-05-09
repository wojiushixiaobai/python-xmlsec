import contextlib
import html.parser
import json
import multiprocessing
import os
import re
import subprocess
import sys
import tarfile
import zipfile
from distutils import log
from distutils.errors import DistutilsError
from packaging.version import Version
from pathlib import Path
from urllib.parse import urljoin
from urllib.request import Request, urlcleanup, urlopen, urlretrieve


class HrefCollector(html.parser.HTMLParser):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.hrefs = []

    def handle_starttag(self, tag, attrs):
        if tag == 'a':
            for name, value in attrs:
                if name == 'href':
                    self.hrefs.append(value)


def make_request(url, github_token=None, json_response=False):
    headers = {'User-Agent': 'https://github.com/xmlsec/python-xmlsec'}
    if github_token:
        headers['authorization'] = "Bearer " + github_token
    request = Request(url, headers=headers)
    with contextlib.closing(urlopen(request)) as r:
        charset = r.headers.get_content_charset() or 'utf-8'
        content = r.read().decode(charset)
        if json_response:
            return json.loads(content)
        else:
            return content


def latest_release_from_html(url, matcher):
    content = make_request(url)
    collector = HrefCollector()
    collector.feed(content)
    hrefs = collector.hrefs

    def comp(text):
        try:
            return Version(matcher.match(text).groupdict()['version'])
        except (AttributeError, ValueError):
            return Version('0.0')

    latest = max(hrefs, key=comp)
    return '{}/{}'.format(url, latest)


def latest_release_from_gnome_org_cache(url, lib_name):
    cache_url = '{}/cache.json'.format(url)
    cache = make_request(cache_url, json_response=True)
    latest_version = cache[2][lib_name][-1]
    latest_source = cache[1][lib_name][latest_version]['tar.xz']
    return '{}/{}'.format(url, latest_source)


def latest_release_from_github_api(repo):
    api_url = 'https://api.github.com/repos/{}/releases'.format(repo)

    # if we are running in CI, pass along the GH_TOKEN, so we don't get rate limited
    token = os.environ.get("GH_TOKEN")
    if token:
        log.info("Using GitHub token to avoid rate limiting")
    api_releases = make_request(api_url, token, json_response=True)
    releases = [r['tarball_url'] for r in api_releases if r['prerelease'] is False and r['draft'] is False]
    if not releases:
        raise DistutilsError('No release found for {}'.format(repo))
    return releases[0]


def latest_openssl_release():
    return latest_release_from_github_api('openssl/openssl')


def latest_zlib_release():
    return latest_release_from_html('https://zlib.net/fossils', re.compile('zlib-(?P<version>.*).tar.gz'))


def latest_libiconv_release():
    return latest_release_from_html('https://ftp.gnu.org/pub/gnu/libiconv', re.compile('libiconv-(?P<version>.*).tar.gz'))


def latest_libxml2_release():
    return latest_release_from_gnome_org_cache('https://download.gnome.org/sources/libxml2', 'libxml2')


def latest_libxslt_release():
    return latest_release_from_gnome_org_cache('https://download.gnome.org/sources/libxslt', 'libxslt')


def latest_xmlsec_release():
    return latest_release_from_html('https://www.aleksey.com/xmlsec/download/', re.compile('xmlsec1-(?P<version>.*).tar.gz'))


class CrossCompileInfo:
    def __init__(self, host, arch, compiler):
        self.host = host
        self.arch = arch
        self.compiler = compiler

    @property
    def triplet(self):
        return "{}-{}-{}".format(self.host, self.arch, self.compiler)


class DependencyBuilder:
    def __init__(self):
        self.debug = os.environ.get('PYXMLSEC_ENABLE_DEBUG', False)
        self.static = os.environ.get('PYXMLSEC_STATIC_DEPS', False)
        self.size_opt = os.environ.get('PYXMLSEC_OPTIMIZE_SIZE', True)

        if self.static or sys.platform == 'win32':
            log.info('starting static build on {}'.format(sys.platform))
            buildroot = Path('/host', 'tmp', 'xmlsec.build')

            self.prefix_dir = buildroot / 'prefix'
            self.prefix_dir.mkdir(parents=True, exist_ok=True)
            self.prefix_dir = self.prefix_dir.absolute()

            self.build_libs_dir = buildroot / 'libs'
            self.build_libs_dir.mkdir(exist_ok=True)

            self.libs_dir = Path(os.environ.get('PYXMLSEC_LIBS_DIR', 'libs'))
            self.libs_dir.mkdir(exist_ok=True)
            log.info('{:20} {}'.format('Lib sources in:', self.libs_dir.absolute()))

            if sys.platform == 'win32':
                self.prepare_static_build_win()
            elif 'linux' in sys.platform:
                self.prepare_static_build(sys.platform)
            elif 'darwin' in sys.platform:
                self.prepare_static_build(sys.platform)

    def prepare_static_build_win(self):
        release_url = 'https://github.com/mxamin/python-xmlsec-win-binaries/releases/download/2024.04.17/'
        if sys.maxsize > 2147483647:  # 2.0 GiB
            suffix = 'win64'
        else:
            suffix = 'win32'

        libs = [
            'libxml2-2.11.7.{}.zip'.format(suffix),
            'libxslt-1.1.37.{}.zip'.format(suffix),
            'zlib-1.2.12.{}.zip'.format(suffix),
            'iconv-1.16-1.{}.zip'.format(suffix),
            'openssl-3.0.8.{}.zip'.format(suffix),
            'xmlsec-1.3.4.{}.zip'.format(suffix),
        ]

        for libfile in libs:
            url = urljoin(release_url, libfile)
            destfile = self.libs_dir / libfile
            if destfile.is_file():
                log.info('Using local copy of "{}"'.format(url))
            else:
                log.info('Retrieving "{}" to "{}"'.format(url, destfile))
                urlcleanup()  # work around FTP bug 27973 in Py2.7.12+
                urlretrieve(url, str(destfile))

        for p in self.libs_dir.glob('*.zip'):
            with zipfile.ZipFile(str(p)) as f:
                destdir = self.build_libs_dir
                f.extractall(path=str(destdir))

        includes = [p for p in self.build_libs_dir.rglob('include') if p.is_dir()]
        includes.append(next(p / 'xmlsec' for p in includes if (p / 'xmlsec').is_dir()))

    def prepare_static_build(self, build_platform):
        self.openssl_version = os.environ.get('PYXMLSEC_OPENSSL_VERSION')
        self.libiconv_version = os.environ.get('PYXMLSEC_LIBICONV_VERSION')
        self.libxml2_version = os.environ.get('PYXMLSEC_LIBXML2_VERSION')
        self.libxslt_version = os.environ.get('PYXMLSEC_LIBXSLT_VERSION')
        self.zlib_version = os.environ.get('PYXMLSEC_ZLIB_VERSION')
        self.xmlsec1_version = os.environ.get('PYXMLSEC_XMLSEC1_VERSION')

        # fetch openssl
        openssl_tar = next(self.libs_dir.glob('openssl*.tar.gz'), None)
        if openssl_tar is None:
            log.info('{:10}: {}'.format('OpenSSL', 'source tar not found, downloading ...'))
            openssl_tar = self.libs_dir / 'openssl.tar.gz'
            if self.openssl_version is None:
                url = latest_openssl_release()
                log.info('{:10}: {}'.format('OpenSSL', 'PYXMLSEC_OPENSSL_VERSION unset, downloading latest from {}'.format(url)))
            else:
                url = 'https://api.github.com/repos/openssl/openssl/tarball/openssl-{}'.format(self.openssl_version)
                log.info('{:10}: {} {}'.format('OpenSSL', 'version', self.openssl_version))
            urlretrieve(url, str(openssl_tar))

        # fetch zlib
        zlib_tar = next(self.libs_dir.glob('zlib*.tar.gz'), None)
        if zlib_tar is None:
            log.info('{:10}: {}'.format('zlib', 'source not found, downloading ...'))
            zlib_tar = self.libs_dir / 'zlib.tar.gz'
            if self.zlib_version is None:
                url = latest_zlib_release()
                log.info('{:10}: {}'.format('zlib', 'PYXMLSEC_ZLIB_VERSION unset, downloading latest from {}'.format(url)))
            else:
                url = 'https://zlib.net/fossils/zlib-{}.tar.gz'.format(self.zlib_version)
                log.info(
                    '{:10}: {}'.format('zlib', 'PYXMLSEC_ZLIB_VERSION={}, downloading from {}'.format(self.zlib_version, url))
                )
            urlretrieve(url, str(zlib_tar))

        # fetch libiconv
        libiconv_tar = next(self.libs_dir.glob('libiconv*.tar.gz'), None)
        if libiconv_tar is None:
            log.info('{:10}: {}'.format('libiconv', 'source not found, downloading ...'))
            libiconv_tar = self.libs_dir / 'libiconv.tar.gz'
            if self.libiconv_version is None:
                url = latest_libiconv_release()
                log.info('{:10}: {}'.format('zlib', 'PYXMLSEC_LIBICONV_VERSION unset, downloading latest from {}'.format(url)))
            else:
                url = 'https://ftp.gnu.org/pub/gnu/libiconv/libiconv-{}.tar.gz'.format(self.libiconv_version)
                log.info(
                    '{:10}: {}'.format(
                        'zlib', 'PYXMLSEC_LIBICONV_VERSION={}, downloading from {}'.format(self.libiconv_version, url)
                    )
                )
            urlretrieve(url, str(libiconv_tar))

        # fetch libxml2
        libxml2_tar = next(self.libs_dir.glob('libxml2*.tar.xz'), None)
        if libxml2_tar is None:
            log.info('{:10}: {}'.format('libxml2', 'source tar not found, downloading ...'))
            if self.libxml2_version is None:
                url = latest_libxml2_release()
                log.info('{:10}: {}'.format('libxml2', 'PYXMLSEC_LIBXML2_VERSION unset, downloading latest from {}'.format(url)))
            else:
                version_prefix, _ = self.libxml2_version.rsplit('.', 1)
                url = 'https://download.gnome.org/sources/libxml2/{}/libxml2-{}.tar.xz'.format(
                    version_prefix, self.libxml2_version
                )
                log.info(
                    '{:10}: {}'.format(
                        'libxml2', 'PYXMLSEC_LIBXML2_VERSION={}, downloading from {}'.format(self.libxml2_version, url)
                    )
                )
            libxml2_tar = self.libs_dir / 'libxml2.tar.xz'
            urlretrieve(url, str(libxml2_tar))

        # fetch libxslt
        libxslt_tar = next(self.libs_dir.glob('libxslt*.tar.gz'), None)
        if libxslt_tar is None:
            log.info('{:10}: {}'.format('libxslt', 'source tar not found, downloading ...'))
            if self.libxslt_version is None:
                url = latest_libxslt_release()
                log.info('{:10}: {}'.format('libxslt', 'PYXMLSEC_LIBXSLT_VERSION unset, downloading latest from {}'.format(url)))
            else:
                version_prefix, _ = self.libxslt_version.rsplit('.', 1)
                url = 'https://download.gnome.org/sources/libxslt/{}/libxslt-{}.tar.xz'.format(
                    version_prefix, self.libxslt_version
                )
                log.info(
                    '{:10}: {}'.format(
                        'libxslt', 'PYXMLSEC_LIBXSLT_VERSION={}, downloading from {}'.format(self.libxslt_version, url)
                    )
                )
            libxslt_tar = self.libs_dir / 'libxslt.tar.gz'
            urlretrieve(url, str(libxslt_tar))

        # fetch xmlsec1
        xmlsec1_tar = next(self.libs_dir.glob('xmlsec1*.tar.gz'), None)
        if xmlsec1_tar is None:
            log.info('{:10}: {}'.format('xmlsec1', 'source tar not found, downloading ...'))
            if self.xmlsec1_version is None:
                url = latest_xmlsec_release()
                log.info('{:10}: {}'.format('xmlsec1', 'PYXMLSEC_XMLSEC1_VERSION unset, downloading latest from {}'.format(url)))
            else:
                url = 'https://www.aleksey.com/xmlsec/download/xmlsec1-{}.tar.gz'.format(self.xmlsec1_version)
                log.info(
                    '{:10}: {}'.format(
                        'xmlsec1', 'PYXMLSEC_XMLSEC1_VERSION={}, downloading from {}'.format(self.xmlsec1_version, url)
                    )
                )
            xmlsec1_tar = self.libs_dir / 'xmlsec1.tar.gz'
            headers = {'User-Agent': 'https://github.com/xmlsec/python-xmlsec'}
            request = Request(url, headers=headers)
            with urlopen(request) as response, open(str(xmlsec1_tar), 'wb') as out_file:
                out_file.write(response.read())
            # urlretrieve(url, str(xmlsec1_tar))

        for file in (openssl_tar, zlib_tar, libiconv_tar, libxml2_tar, libxslt_tar, xmlsec1_tar):
            log.info('Unpacking {}'.format(file.name))
            try:
                with tarfile.open(str(file)) as tar:
                    tar.extractall(path=str(self.build_libs_dir))
            except EOFError:
                raise DistutilsError('Bad {} downloaded; remove it and try again.'.format(file.name))

        prefix_arg = '--prefix={}'.format(self.prefix_dir)

        env = os.environ.copy()
        cflags = []
        if env.get('CFLAGS'):
            cflags.append(env['CFLAGS'])
        cflags.append('-fPIC')
        ldflags = []
        if env.get('LDFLAGS'):
            ldflags.append(env['LDFLAGS'])

        cross_compiling = False
        if build_platform == 'darwin':
            import platform

            arch = self.plat_name.rsplit('-', 1)[1]
            if arch != platform.machine() and arch in ('x86_64', 'arm64'):
                log.info('Cross-compiling for {}'.format(arch))
                cflags.append('-arch {}'.format(arch))
                ldflags.append('-arch {}'.format(arch))
                cross_compiling = CrossCompileInfo('darwin64', arch, 'cc')
            major_version, minor_version = tuple(map(int, platform.mac_ver()[0].split('.')[:2]))
            if major_version >= 11:
                if 'MACOSX_DEPLOYMENT_TARGET' not in env:
                    env['MACOSX_DEPLOYMENT_TARGET'] = "11.0"

        env['CFLAGS'] = ' '.join(cflags)
        env['LDFLAGS'] = ' '.join(ldflags)

        log.info('Building OpenSSL')
        openssl_dir = next(self.build_libs_dir.glob('openssl-*'))
        openssl_config_cmd = [prefix_arg, 'no-shared', '-fPIC', '--libdir=lib']
        if cross_compiling:
            openssl_config_cmd.insert(0, './Configure')
            openssl_config_cmd.append(cross_compiling.triplet)
        else:
            openssl_config_cmd.insert(0, './config')
        subprocess.check_output(openssl_config_cmd, cwd=str(openssl_dir), env=env)
        subprocess.check_output(['make', '-j{}'.format(multiprocessing.cpu_count() + 1)], cwd=str(openssl_dir), env=env)
        subprocess.check_output(
            ['make', '-j{}'.format(multiprocessing.cpu_count() + 1), 'install_sw'], cwd=str(openssl_dir), env=env
        )

        log.info('Building zlib')
        zlib_dir = next(self.build_libs_dir.glob('zlib-*'))
        subprocess.check_output(['./configure', prefix_arg], cwd=str(zlib_dir), env=env)
        subprocess.check_output(['make', '-j{}'.format(multiprocessing.cpu_count() + 1)], cwd=str(zlib_dir), env=env)
        subprocess.check_output(['make', '-j{}'.format(multiprocessing.cpu_count() + 1), 'install'], cwd=str(zlib_dir), env=env)

        host_arg = ""
        if cross_compiling:
            host_arg = '--host={}'.format(cross_compiling.arch)

        log.info('Building libiconv')
        libiconv_dir = next(self.build_libs_dir.glob('libiconv-*'))
        subprocess.check_output(
            [
                './configure',
                prefix_arg,
                '--disable-dependency-tracking',
                '--disable-shared',
                host_arg,
            ],
            cwd=str(libiconv_dir),
            env=env,
        )
        subprocess.check_output(['make', '-j{}'.format(multiprocessing.cpu_count() + 1)], cwd=str(libiconv_dir), env=env)
        subprocess.check_output(
            ['make', '-j{}'.format(multiprocessing.cpu_count() + 1), 'install'], cwd=str(libiconv_dir), env=env
        )

        log.info('Building LibXML2')
        libxml2_dir = next(self.build_libs_dir.glob('libxml2-*'))
        subprocess.check_output(
            [
                './configure',
                prefix_arg,
                '--disable-dependency-tracking',
                '--disable-shared',
                '--without-lzma',
                '--without-python',
                '--with-iconv={}'.format(self.prefix_dir),
                '--with-zlib={}'.format(self.prefix_dir),
                host_arg,
            ],
            cwd=str(libxml2_dir),
            env=env,
        )
        subprocess.check_output(['make', '-j{}'.format(multiprocessing.cpu_count() + 1)], cwd=str(libxml2_dir), env=env)
        subprocess.check_output(
            ['make', '-j{}'.format(multiprocessing.cpu_count() + 1), 'install'], cwd=str(libxml2_dir), env=env
        )

        log.info('Building libxslt')
        libxslt_dir = next(self.build_libs_dir.glob('libxslt-*'))
        subprocess.check_output(
            [
                './configure',
                prefix_arg,
                '--disable-dependency-tracking',
                '--disable-shared',
                '--without-python',
                '--without-crypto',
                '--with-libxml-prefix={}'.format(self.prefix_dir),
                host_arg,
            ],
            cwd=str(libxslt_dir),
            env=env,
        )
        subprocess.check_output(['make', '-j{}'.format(multiprocessing.cpu_count() + 1)], cwd=str(libxslt_dir), env=env)
        subprocess.check_output(
            ['make', '-j{}'.format(multiprocessing.cpu_count() + 1), 'install'], cwd=str(libxslt_dir), env=env
        )

        log.info('Building xmlsec1')
        ldflags.append('-lpthread')
        env['LDFLAGS'] = ' '.join(ldflags)
        xmlsec1_dir = next(self.build_libs_dir.glob('xmlsec1-*'))
        subprocess.check_output(
            [
                './configure',
                prefix_arg,
                '--disable-shared',
                '--disable-gost',
                '--enable-md5',
                '--disable-crypto-dl',
                '--enable-static=yes',
                '--enable-shared=no',
                '--enable-static-linking=yes',
                '--with-default-crypto=openssl',
                '--with-openssl={}'.format(self.prefix_dir),
                '--with-libxml={}'.format(self.prefix_dir),
                '--with-libxslt={}'.format(self.prefix_dir),
                host_arg,
            ],
            cwd=str(xmlsec1_dir),
            env=env,
        )
        subprocess.check_output(
            ['make', '-j{}'.format(multiprocessing.cpu_count() + 1)]
            + ['-I{}'.format(str(self.prefix_dir / 'include')), '-I{}'.format(str(self.prefix_dir / 'include' / 'libxml'))],
            cwd=str(xmlsec1_dir),
            env=env,
        )
        subprocess.check_output(
            ['make', '-j{}'.format(multiprocessing.cpu_count() + 1), 'install'], cwd=str(xmlsec1_dir), env=env
        )

    def build(self):
        if sys.platform == 'win32':
            self.prepare_static_build_win()
        else:
            self.prepare_static_build(sys.platform)


if __name__ == '__main__':
    log.set_verbosity(1)
    builder = DependencyBuilder()
    builder.build()
