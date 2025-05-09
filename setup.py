import io
import os
import sys
from distutils import log
from distutils.errors import DistutilsError
from pathlib import Path

from setuptools import Extension, setup
from setuptools.command.build_ext import build_ext as build_ext_orig

class build_ext(build_ext_orig):
    def info(self, message):
        self.announce(message, level=log.INFO)

    def run(self):
        ext = self.ext_map['xmlsec']
        self.debug = os.environ.get('PYXMLSEC_ENABLE_DEBUG', False)
        self.static = os.environ.get('PYXMLSEC_STATIC_DEPS', False)
        self.size_opt = os.environ.get('PYXMLSEC_OPTIMIZE_SIZE', True)

        if self.static or sys.platform == 'win32':
            self.info('starting static build on {}'.format(sys.platform))
            buildroot = Path('build', 'tmp')

            self.prefix_dir = buildroot / 'prefix'
            self.prefix_dir.mkdir(parents=True, exist_ok=True)
            self.prefix_dir = self.prefix_dir.absolute()

            self.build_libs_dir = buildroot / 'libs'
            self.build_libs_dir.mkdir(exist_ok=True)

            self.libs_dir = Path(os.environ.get('PYXMLSEC_LIBS_DIR', 'libs'))
            self.libs_dir.mkdir(exist_ok=True)
            self.info('{:20} {}'.format('Lib sources in:', self.libs_dir.absolute()))

            if sys.platform == 'win32':
                self.prepare_static_build_win()
            elif 'linux' in sys.platform:
                self.prepare_static_build(sys.platform)
            elif 'darwin' in sys.platform:
                self.prepare_static_build(sys.platform)
        else:
            import pkgconfig

            try:
                config = pkgconfig.parse('xmlsec1')
            except EnvironmentError:
                raise DistutilsError('Unable to invoke pkg-config.')
            except pkgconfig.PackageNotFoundError:
                raise DistutilsError('xmlsec1 is not installed or not in path.')

            if config is None or not config.get('libraries'):
                raise DistutilsError('Bad or incomplete result returned from pkg-config.')

            ext.define_macros.extend(config['define_macros'])
            ext.include_dirs.extend(config['include_dirs'])
            ext.library_dirs.extend(config['library_dirs'])
            ext.libraries.extend(config['libraries'])

        import lxml

        ext.include_dirs.extend(lxml.get_include())

        ext.define_macros.extend(
            [('MODULE_NAME', self.distribution.metadata.name), ('MODULE_VERSION', self.distribution.metadata.version)]
        )
        # escape the XMLSEC_CRYPTO macro value, see mehcode/python-xmlsec#141
        for key, value in ext.define_macros:
            if key == 'XMLSEC_CRYPTO' and not (value.startswith('"') and value.endswith('"')):
                ext.define_macros.remove((key, value))
                ext.define_macros.append((key, '"{0}"'.format(value)))
                break

        if sys.platform == 'win32':
            ext.extra_compile_args.append('/Zi')
        else:
            ext.extra_compile_args.extend(
                [
                    '-g',
                    '-std=c99',
                    '-fPIC',
                    '-fno-strict-aliasing',
                    '-Wno-error=declaration-after-statement',
                    '-Werror=implicit-function-declaration',
                ]
            )

        if self.debug:
            ext.define_macros.append(('PYXMLSEC_ENABLE_DEBUG', '1'))
            if sys.platform == 'win32':
                ext.extra_compile_args.append('/Od')
            else:
                ext.extra_compile_args.append('-Wall')
                ext.extra_compile_args.append('-O0')
        else:
            if self.size_opt:
                if sys.platform == 'win32':
                    ext.extra_compile_args.append('/Os')
                else:
                    ext.extra_compile_args.append('-Os')

        super(build_ext, self).run()

    def prepare_static_build_win(self):
        ext = self.ext_map['xmlsec']
        ext.define_macros = [
            ('XMLSEC_CRYPTO', '\\"openssl\\"'),
            ('__XMLSEC_FUNCTION__', '__FUNCTION__'),
            ('XMLSEC_NO_GOST', '1'),
            ('XMLSEC_NO_XKMS', '1'),
            ('XMLSEC_NO_CRYPTO_DYNAMIC_LOADING', '1'),
            ('XMLSEC_CRYPTO_OPENSSL', '1'),
            ('UNICODE', '1'),
            ('_UNICODE', '1'),
            ('LIBXML_ICONV_ENABLED', 1),
            ('LIBXML_STATIC', '1'),
            ('LIBXSLT_STATIC', '1'),
            ('XMLSEC_STATIC', '1'),
            ('inline', '__inline'),
        ]
        ext.libraries = [
            'libxmlsec_a',
            'libxmlsec-openssl_a',
            'libcrypto',
            'iconv_a',
            'libxslt_a',
            'libexslt_a',
            'libxml2_a',
            'zlib',
            'WS2_32',
            'Advapi32',
            'User32',
            'Gdi32',
            'Crypt32',
        ]
        ext.library_dirs = [str(p.absolute()) for p in self.build_libs_dir.rglob('lib')]

        includes = [p for p in self.build_libs_dir.rglob('include') if p.is_dir()]
        includes.append(next(p / 'xmlsec' for p in includes if (p / 'xmlsec').is_dir()))
        ext.include_dirs = [str(p.absolute()) for p in includes]

    def prepare_static_build(self, build_platform):
        ext = self.ext_map['xmlsec']
        ext.define_macros = [
            ('__XMLSEC_FUNCTION__', '__func__'),
            ('XMLSEC_NO_SIZE_T', None),
            ('XMLSEC_NO_GOST', '1'),
            ('XMLSEC_NO_GOST2012', '1'),
            ('XMLSEC_NO_XKMS', '1'),
            ('XMLSEC_CRYPTO', '\\"openssl\\"'),
            ('XMLSEC_NO_CRYPTO_DYNAMIC_LOADING', '1'),
            ('XMLSEC_CRYPTO_OPENSSL', '1'),
            ('LIBXML_ICONV_ENABLED', 1),
            ('LIBXML_STATIC', '1'),
            ('LIBXSLT_STATIC', '1'),
            ('XMLSEC_STATIC', '1'),
            ('inline', '__inline'),
            ('UNICODE', '1'),
            ('_UNICODE', '1'),
        ]

        ext.include_dirs.append(str(self.prefix_dir / 'include'))
        ext.include_dirs.extend([str(p.absolute()) for p in (self.prefix_dir / 'include').iterdir() if p.is_dir()])

        ext.library_dirs = []
        if build_platform == 'linux':
            ext.libraries = ['m', 'rt']
        extra_objects = [
            'libxmlsec1.a',
            'libxslt.a',
            'libxml2.a',
            'libz.a',
            'libxmlsec1-openssl.a',
            'libcrypto.a',
            'libiconv.a',
            'libxmlsec1.a',
        ]
        ext.extra_objects = [str(self.prefix_dir / 'lib' / o) for o in extra_objects]


src_root = Path(__file__).parent / 'src'
sources = [str(p.absolute()) for p in src_root.rglob('*.c')]
pyxmlsec = Extension('xmlsec', sources=sources)
setup_reqs = ['setuptools_scm[toml]>=3.4', 'pkgconfig>=1.5.1', 'lxml>=3.8']


with io.open('README.rst', encoding='utf-8') as f:
    long_desc = f.read()


setup(
    name='xmlsec',
    use_scm_version=True,
    description='Python bindings for the XML Security Library',
    long_description=long_desc,
    long_description_content_type='text/markdown',
    ext_modules=[pyxmlsec],
    cmdclass={'build_ext': build_ext},
    python_requires='>=3.5',
    setup_requires=setup_reqs,
    install_requires=['lxml>=3.8'],
    author="Bulat Gaifullin",
    author_email='support@mehcode.com',
    maintainer='Oleg Hoefling',
    maintainer_email='oleg.hoefling@gmail.com',
    url='https://github.com/mehcode/python-xmlsec',
    project_urls={
        'Documentation': 'https://xmlsec.readthedocs.io',
        'Source': 'https://github.com/mehcode/python-xmlsec',
        'Changelog': 'https://github.com/mehcode/python-xmlsec/releases',
    },
    license='MIT',
    keywords=['xmlsec'],
    classifiers=[
        'Development Status :: 5 - Production/Stable',
        'Intended Audience :: Developers',
        'Intended Audience :: System Administrators',
        'License :: OSI Approved :: MIT License',
        'Operating System :: OS Independent',
        'Programming Language :: C',
        'Programming Language :: Python :: 3',
        'Programming Language :: Python :: 3.5',
        'Programming Language :: Python :: 3.6',
        'Programming Language :: Python :: 3.7',
        'Programming Language :: Python :: 3.8',
        'Programming Language :: Python :: 3.9',
        'Programming Language :: Python :: 3.11',
        'Topic :: Text Processing :: Markup :: XML',
        'Typing :: Typed',
    ],
    zip_safe=False,
    packages=['xmlsec'],
    package_dir={'': 'src'},
    package_data={'xmlsec': ['py.typed', '*.pyi']},
)
