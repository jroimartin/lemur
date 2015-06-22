"""
.. module: service
    :platform: Unix
    :copyright: (c) 2015 by Netflix Inc., see AUTHORS for more
    :license: Apache, see LICENSE for more details.
.. moduleauthor:: Kevin Glisson <kglisson@netflix.com>
"""
import os
import arrow
import string
import random
import hashlib
import datetime
import subprocess

from sqlalchemy import func, or_
from flask import g, current_app

from lemur import database
from lemur.common.services.aws import iam
from lemur.common.services.issuers.manager import get_plugin_by_name

from lemur.certificates.models import Certificate
from lemur.certificates.exceptions import UnableToCreateCSR, \
    UnableToCreatePrivateKey, MissingFiles

from lemur.accounts.models import Account
from lemur.accounts import service as account_service
from lemur.authorities.models import Authority

from lemur.roles.models import Role


def get(cert_id):
    """
    Retrieves certificate by it's ID.

    :param cert_id:
    :return:
    """
    return database.get(Certificate, cert_id)


def get_by_name(name):
    """
    Retrieves certificate by it's Name.

    :param name:
    :return:
    """
    return database.get(Certificate, name, field='name')


def delete(cert_id):
    """
    Delete's a certificate.

    :param cert_id:
    """
    database.delete(get(cert_id))


def disassociate_aws_account(certs, account):
    """
    Removes the account association from a certificate. We treat AWS as a completely
    external service. Certificates are added and removed from this service but a record
    of that certificate is always kept and tracked by Lemur. This allows us to migrate
    certificates to different accounts with ease.

    :param certs:
    :param account:
    """
    account_certs = Certificate.query.filter(Certificate.accounts.any(Account.id == 1)).\
                        filter(~Certificate.body.in_(certs)).all()

    for a_cert in account_certs:
        try:
            a_cert.accounts.remove(account)
        except Exception as e:
            current_app.logger.debug("Skipping {0} account {1} is already disassociated".format(a_cert.name, account.label))
            continue
        database.update(a_cert)


def get_all_certs():
    """
    Retrieves all certificates within Lemur.

    :return:
    """
    return Certificate.query.all()


def find_duplicates(cert_body):
    """
    Finds certificates that already exist within Lemur. We do this by looking for
    certificate bodies that are the same. This is the most reliable way to determine
    if a certificate is already being tracked by Lemur.

    :param cert_body:
    :return:
    """
    return Certificate.query.filter_by(body=cert_body).all()


def update(cert_id, owner, active):
    """
    Updates a certificate.

    :param cert_id:
    :param owner:
    :param active:
    :return:
    """
    cert = get(cert_id)
    cert.owner = owner
    cert.active = active
    return database.update(cert)


def mint(issuer_options):
    """
    Minting is slightly different for each authority.
    Support for multiple authorities is handled by individual plugins.

    :param issuer_options:
    """
    authority = issuer_options['authority']

    issuer = get_plugin_by_name(authority.plugin_name)
    # NOTE if we wanted to support more issuers it might make sense to
    # push CSR creation down to the plugin
    path = create_csr(issuer.get_csr_config(issuer_options))
    challenge, csr, csr_config, private_key = load_ssl_pack(path)

    issuer_options['challenge'] = challenge
    issuer_options['creator'] = g.user.email
    cert_body, cert_chain = issuer.create_certificate(csr, issuer_options)

    cert = save_cert(cert_body, private_key, cert_chain, challenge, csr_config, issuer_options.get('accounts'))
    cert.user = g.user
    cert.authority = authority
    database.update(cert)

    # securely delete pack after saving it to RDS and IAM (if applicable)
    delete_ssl_pack(path)

    return cert, private_key, cert_chain,


def import_certificate(**kwargs):
    """
    Uploads already minted certificates and pulls the required information into Lemur.

    This is to be used for certificates that are reated outside of Lemur but
    should still be tracked.

    Internally this is used to bootstrap Lemur with external
    certificates, and used when certificates are 'discovered' through various discovery
    techniques. was still in aws.

    :param kwargs:
    """
    cert = Certificate(kwargs['public_certificate'])
    cert.owner = kwargs.get('owner', )
    cert.creator = kwargs.get('creator', 'Lemur')

    # NOTE existing certs may not follow our naming standard we will
    # overwrite the generated name with the actual cert name
    if kwargs.get('name'):
        cert.name = kwargs.get('name')

    if kwargs.get('user'):
        cert.user = kwargs.get('user')

    if kwargs.get('account'):
        cert.accounts.append(kwargs.get('account'))

    cert = database.create(cert)
    return cert


def save_cert(cert_body, private_key, cert_chain, challenge, csr_config, accounts):
    """
    Determines if the certificate needs to be uploaded to AWS or other services.

    :param cert_body:
    :param private_key:
    :param cert_chain:
    :param challenge:
    :param csr_config:
    :param account_ids:
    """
    cert = Certificate(cert_body, private_key, challenge, cert_chain, csr_config)
    # if we have an AWS accounts lets upload them
    if accounts:
        for account in accounts:
            account = account_service.get(account['id'])
            iam.upload_cert(account.account_number, cert, private_key, cert_chain)
            cert.accounts.append(account)
    return cert


def upload(**kwargs):
    """
    Allows for pre-made certificates to be imported into Lemur.
    """
    # save this cert the same way we save all of our certs, including uploading
    # to aws if necessary
    cert = save_cert(
        kwargs.get('public_cert'),
        kwargs.get('private_key'),
        kwargs.get('intermediate_cert'),
        None,
        None,
        kwargs.get('accounts')
    )

    cert.owner = kwargs['owner']
    cert = database.create(cert)
    g.user.certificates.append(cert)
    return cert


def create(**kwargs):
    """
    Creates a new certificate.
    """
    cert, private_key, cert_chain = mint(kwargs)

    cert.owner = kwargs['owner']
    database.create(cert)
    g.user.certificates.append(cert)
    database.update(g.user)
    return cert


def render(args):
    """
    Helper function that allows use to render our REST Api.

    :param args:
    :return:
    """
    query = database.session_query(Certificate)

    time_range = args.pop('time_range')
    account_id = args.pop('account_id')
    show = args.pop('show')
    owner = args.pop('owner')
    creator = args.pop('creator')  # TODO we should enabling filtering by owner

    filt = args.pop('filter')

    if filt:
        terms = filt.split(';')
        if 'issuer' in terms:
            # we can't rely on issuer being correct in the cert directly so we combine queries
            sub_query = database.session_query(Authority.id)\
                .filter(Authority.name.ilike('%{0}%'.format(terms[1])))\
                .subquery()

            query = query.filter(
                or_(
                    Certificate.issuer.ilike('%{0}%'.format(terms[1])),
                    Certificate.authority_id.in_(sub_query)
                )
            )
            return database.sort_and_page(query, Certificate, args)

        if 'account' in terms:
            query = query.filter(Certificate.accounts.any(Account.id == terms[1]))
        elif 'active' in filt: # this is really weird but strcmp seems to not work here??
            query = query.filter(Certificate.active == terms[1])
        else:
            query = database.filter(query, Certificate, terms)

    if show:
        sub_query = database.session_query(Role.name).filter(Role.user_id == g.user.id).subquery()
        query = query.filter(
            or_(
                Certificate.user_id == g.user.id,
                Certificate.owner.in_(sub_query)
            )
        )

    if account_id:
        query = query.filter(Certificate.accounts.any(Account.id == account_id))

    if time_range:
        to = arrow.now().replace(weeks=+time_range).format('YYYY-MM-DD')
        now = arrow.now().format('YYYY-MM-DD')
        query = query.filter(Certificate.not_after <= to).filter(Certificate.not_after >= now)

    return database.sort_and_page(query, Certificate, args)


def create_csr(csr_config):
    """
    Given a list of domains create the appropriate csr
    for those domains

    :param csr_config:
    """

    # we create a no colliding file name
    path = create_path(hashlib.md5(csr_config).hexdigest())

    challenge = create_challenge()
    challenge_path = os.path.join(path, 'challenge.txt')

    with open(challenge_path, 'w') as c:
        c.write(challenge)

    csr_path = os.path.join(path, 'csr_config.txt')

    with open(csr_path, 'w') as f:
        f.write(csr_config)

    #TODO use cloudCA to seed a -rand file for each call
    #TODO replace openssl shell calls with cryptograph
    with open('/dev/null', 'w') as devnull:
        code = subprocess.call(['openssl', 'genrsa',
                                '-out', os.path.join(path, 'private.key'), '2048'],
                               stdout=devnull, stderr=devnull)

        if code != 0:
            raise UnableToCreatePrivateKey(code)

    with open('/dev/null', 'w') as devnull:
        code = subprocess.call(['openssl', 'req', '-new', '-sha256', '-nodes',
                                            '-config', csr_path, "-key", os.path.join(path, 'private.key'),
                                            "-out", os.path.join(path, 'request.csr')], stdout=devnull, stderr=devnull)

        if code != 0:
            raise UnableToCreateCSR(code)

    return path


def create_path(domain_hash):
    """

    :param domain_hash:
    :return:
    """
    path = os.path.join('/tmp', domain_hash)

    try:
        os.mkdir(path)
    except OSError as e:
        now = datetime.datetime.now()
        path = os.path.join('/tmp', "{}.{}".format(domain_hash, now.strftime('%s')))
        os.mkdir(path)
        current_app.logger.warning(e)

    current_app.logger.debug("Writing ssl files to: {}".format(path))
    return path


def load_ssl_pack(path):
    """
    Loads the information created by openssl to be used by other functions.

    :param path:
    """
    if len(os.listdir(path)) != 4:
        raise MissingFiles(path)

    with open(os.path.join(path, 'challenge.txt')) as c:
        challenge = c.read()

    with open(os.path.join(path, 'request.csr')) as r:
        csr = r.read()

    with open(os.path.join(path, 'csr_config.txt')) as config:
        csr_config = config.read()

    with open(os.path.join(path, 'private.key')) as key:
        private_key = key.read()

    return (challenge, csr, csr_config, private_key,)


def delete_ssl_pack(path):
    """
    Removes the temporary files associated with CSR creation.

    :param path:
    """
    subprocess.check_call(['srm', '-r', path])


def create_challenge():
    """
    Create a random and strongish csr challenge.
    """
    challenge = ''.join(random.choice(string.ascii_uppercase) for x in range(6))
    challenge += ''.join(random.choice("~!@#$%^&*()_+") for x in range(6))
    challenge += ''.join(random.choice(string.ascii_lowercase) for x in range(6))
    challenge += ''.join(random.choice(string.digits) for x in range(6))
    return challenge


def stats(**kwargs):
    """
    Helper that defines some useful statistics about certifications.

    :param kwargs:
    :return:
    """
    query = database.session_query(Certificate)

    if kwargs.get('active') == 'true':
        query = query.filter(Certificate.elb_listeners.any())

    if kwargs.get('account_id'):
        query = query.filter(Certificate.accounts.any(Account.id == kwargs.get('account_id')))

    if kwargs.get('metric') == 'not_after':
        start = arrow.utcnow()
        end = start.replace(weeks=+32)
        items = database.db.session.query(Certificate.issuer, func.count(Certificate.id))\
            .group_by(Certificate.issuer)\
            .filter(Certificate.not_after <= end.format('YYYY-MM-DD')) \
            .filter(Certificate.not_after >= start.format('YYYY-MM-DD')).all()

    else:
        attr = getattr(Certificate, kwargs.get('metric'))
        query = database.db.session.query(attr, func.count(attr))

        # TODO this could be cleaned up
        if kwargs.get('active') == 'true':
            query = query.filter(Certificate.elb_listeners.any())

        items = query.group_by(attr).all()

    keys = []
    values = []
    for key, count in items:
        keys.append(key)
        values.append(count)

    return {'labels': keys, 'values': values}

