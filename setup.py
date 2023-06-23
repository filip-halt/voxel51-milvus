import setuptools

setuptools.setup(
    name='voxel51_milvus',
    version='1.0.0',
    description='Voxel51 Milvus Integration.',
    author='Filip Haltmayer',
    author_email='filip@zilliz.com',
    url='https://github.com/filip-halt/voxel51-milvus',
    license="Apache-2.0",
    packages=setuptools.find_packages(),
    include_package_data=True,
    install_requires=[
        "pymilvus==2.2.9",
    ],
    python_requires='>=3.7'
)