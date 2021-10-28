import ldap3
import impacket
import logging
from impacket.ldap import ldaptypes
from impacket.dcerpc.v5 import samr

from .exceptions import NoResultError, ResultError, TooManyResultsError


LOG = logging.getLogger()
logging.basicConfig(level=logging.DEBUG, format='%(message)s')


# 983551 Full control
def createACE(sid, privguid=None, accesstype=983551):
    nace = ldaptypes.ACE()
    nace['AceType'] = ldaptypes.ACCESS_ALLOWED_OBJECT_ACE.ACE_TYPE
    nace['AceFlags'] = 0x00

    if privguid is None:
        acedata = ldaptypes.ACCESS_ALLOWED_ACE()
    else:
        acedata = ldaptypes.ACCESS_ALLOWED_OBJECT_ACE()
        acedata['ObjectType'] = impacket.uuid.string_to_bin(privguid)
        acedata['InheritedObjectType'] = b''
        acedata['Flags'] = ldaptypes.ACCESS_ALLOWED_OBJECT_ACE.ACE_OBJECT_TYPE_PRESENT

    acedata['Mask'] = ldaptypes.ACCESS_MASK()
    acedata['Mask']['Mask'] = accesstype

    if type(sid) is str:
        acedata['Sid'] = ldaptypes.LDAP_SID()
        acedata['Sid'].fromCanonical(sid)
    else:
        acedata['Sid'] = sid

    nace['Ace'] = acedata
    return nace


def createEmptySD():
    sd = ldaptypes.SR_SECURITY_DESCRIPTOR()
    sd['Revision'] = b'\x01'
    sd['Sbz1'] = b'\x00'
    sd['Control'] = 32772
    sd['OwnerSid'] = ldaptypes.LDAP_SID()
    # BUILTIN\Administrators
    sd['OwnerSid'].fromCanonical('S-1-5-32-544')
    sd['GroupSid'] = b''
    sd['Sacl'] = b''
    acl = ldaptypes.ACL()
    acl['AclRevision'] = 4
    acl['Sbz1'] = 0
    acl['Sbz2'] = 0
    acl.aces = []
    sd['Dacl'] = acl
    return sd


def resolvDN(conn, identity):
    """
    Return the DN for the object based on the parameters identity
    The parameter identity can be:
      - a DN, in which case it it not validated and returned as is
      - a sAMAccountName
      - a GUID
      - a SID
    """

    if "dc=" in identity.lower():
        # identity is a DN, return as is
        # We do not try to validate it because it could be from another trusted domain
        return identity

    if "s-1-" in identity.lower():
        # We assume identity is an SID
        ldap_filter = f'(objectSid={identity})'
    elif "{" in identity:
        # We assume identity is a GUID
        ldap_filter = f'(objectGUID={identity})'
    else:
        # By default, we assume identity is a sam account name
        ldap_filter = f'(sAMAccountName={identity})'

    naming_context = getDefaultNamingContext(conn)
    conn.search(naming_context, ldap_filter)

    entries = [e for e in conn.response if e.get('type', '') == 'searchResEntry']

    if len(entries) < 1:
        raise NoResultError(naming_context, ldap_filter)

    if len(entries) > 1:
        raise TooManyResultsError(naming_context, ldap_filter, entries)

    res = entries[0]['dn']
    return res


def getDefaultNamingContext(conn):
    naming_context = conn.server.info.other['defaultNamingContext'][0]
    return naming_context


def cryptPassword(session_key, password):
    try:
        from Cryptodome.Cipher import ARC4
    except Exception:
        LOG.error("Warning: You don't have any crypto installed. You need pycryptodomex")
        LOG.error("See https://pypi.org/project/pycryptodomex/")

    sam_user_pass = samr.SAMPR_USER_PASSWORD()
    encoded_pass = password.encode('utf-16le')
    plen = len(encoded_pass)
    sam_user_pass['Buffer'] = b'A' * (512 - plen) + encoded_pass
    sam_user_pass['Length'] = plen
    pwdBuff = sam_user_pass.getData()

    rc4 = ARC4.new(session_key)
    encBuf = rc4.encrypt(pwdBuff)

    sam_user_pass_enc = samr.SAMPR_ENCRYPTED_USER_PASSWORD()
    sam_user_pass_enc['Buffer'] = encBuf
    return sam_user_pass_enc


def userAccountControl(conn, identity, enable, flag):
    enable = enable == "True"

    user_dn = resolvDN(conn, identity)
    conn.search(user_dn, '(objectClass=*)', attributes=['userAccountControl'])
    entry = conn.entries[0]
    userAccountControl = int(entry["userAccountControl"].value)
    LOG.debug("Original userAccountControl: {userAccountControl}")

    if enable:
        userAccountControl = userAccountControl | flag
    else:
        userAccountControl = userAccountControl & ~flag

    LOG.debug(f"Updated userAccountControl: {userAccountControl}")
    conn.modify(user_dn, {'userAccountControl': (ldap3.MODIFY_REPLACE, [userAccountControl])})

    if conn.result['result'] == 0:
        LOG.info("Updated userAccountControl attribute successfully")
    else:
        raise ResultError(conn.result)


def rpcChangePassword(conn, target, new_pass):
    """
    Change the target password without knowing the old one using RPC instead of LDAPS
    Args:
        domain for NTLM authentication
        NTLM username of the user with permissions on the target
        NTLM password or hash of the user
        IP or hostname of the DC to make the password change
        sAMAccountName of the target
        new password for the target
    """
    dce = conn.getSamrConnection()
    server_handle = samr.hSamrConnect(dce, conn.conf.host + '\x00')['ServerHandle']
    domainSID = samr.hSamrLookupDomainInSamServer(dce, server_handle, conn.conf.domain)['DomainId']
    domain_handle = samr.hSamrOpenDomain(dce, server_handle, domainId=domainSID)['DomainHandle']
    userRID = samr.hSamrLookupNamesInDomain(dce, domain_handle, (target,))['RelativeIds']['Element'][0]
    opened_user = samr.hSamrOpenUser(dce, domain_handle, userId=userRID)

    req = samr.SamrSetInformationUser2()
    req['UserHandle'] = opened_user['UserHandle']
    req['UserInformationClass'] = samr.USER_INFORMATION_CLASS.UserInternal5Information
    req['Buffer'] = samr.SAMPR_USER_INFO_BUFFER()
    req['Buffer']['tag'] = samr.USER_INFORMATION_CLASS.UserInternal5Information
    req['Buffer']['Internal5']['UserPassword'] = cryptPassword(b'SystemLibraryDTC', new_pass)
    req['Buffer']['Internal5']['PasswordExpired'] = 0

    resp = dce.request(req)
    return resp
