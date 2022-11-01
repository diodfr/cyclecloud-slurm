#!/bin/bash -e

apt-get update
apt install -y alien lua5.3 liblua5.3

for rpm in $(ls ~/rpmbuild/RPMS/x86_64/*.rpm ); do
    alien --bump 0 --scripts $rpm
done
mv *.deb ~/rpmbuild/RPMS/x86_64/
