"""
Main workflow engine which carries out the mediation between JPER and the SWORD-enabled repositories
"""
import sword2, uuid
from service import xwalk, models
from octopus.modules.store import store
from octopus.modules.jper import client
from octopus.modules.jper import models as jmod
from StringIO import StringIO
from octopus.modules.swordv2 import client_http
from octopus.core import app
from octopus.lib import dates, http

class DepositException(Exception):
    """
    Generic exception to be thrown in the case of deposit error
    """
    pass

def run(fail_on_error=True):
    """
    Execute a single pass on all the accounts that have sword activated and process all of their
    notifications since the last time their account was synchronised, until now

    :param fail_on_error: cease execution if an exception is raised
    """
    app.logger.info("Entering run")
    # list all of the accounts that have sword activated
    accs = models.Account.with_sword_activated()

    # process each account
    for acc in accs:
        try:
            process_account(acc)
        except client.JPERException as e:
            app.logger.error(u"Problem while processing account for SWORD deposit: {x}".format(x=e.message))
            if fail_on_error:
                raise e
    app.logger.info("Leaving run")

def process_account(acc):
    """
    Retrieve the notifications in JPER associated with this account and relay them on to their sword-enabled repository

    If the account is in status "failing", it will be skipped.

    If the account is in status "problem", and the retry delay has elapsed, it will be re-tried, otherwise it will be skipped

    :param acc: the account whose notifications to process
    """
    app.logger.info(u"Processing Account:{x}".format(x=acc.id))

    # get the current status of the repository
    status = models.RepositoryStatus.pull(acc.id)

    # if no status record is found, this means the repository is new to sword deposit, so we need to create one
    if status is None:
        app.logger.debug(u"Account:{x} has not previously deposited - creating repository status record".format(x=acc.id))
        status = models.RepositoryStatus()
        status.id = acc.id
        status.status = "succeeding"
        status.last_deposit_date = app.config.get("DEFAULT_SINCE_DATE")
        status.save()
    app.logger.info(u"Status:{x}".format(x=status.status))
    # check to see if we should be continuing with this account (may be failing)
    if status.status == "failing":
        app.logger.debug(u"Account:{x} is marked as failing - skipping.  You may need to manually reactivate this account".format(x=acc.id))
        return

    # check to see if enough time has passed to warrant a re-try (if relevant)
    delay = app.config.get("LONG_CYCLE_RETRY_DELAY")
    if status.status == "problem" and not status.can_retry(delay):
        app.logger.debug(u"Account:{x} is experiencing problems, and retry delay has not yet elapsed - skipping".format(x=acc.id))
        return

    # Query JPER for the notifications for this account
    since = status.last_deposit_date
    if since is None:
        since = app.config.get("DEFAULT_SINCE_DATE")
        status.last_deposit_date = since

    j = client.JPER(api_key=acc.api_key)
    try:
        for note in j.iterate_notifications(since, repository_id=acc.id):
            try:
                process_notification(acc, note, since)
                # if the notification is successfully processed, record the new last_deposit_date
                status.last_deposit_date = note.analysis_date
            except DepositException as e:
                app.logger.error(u"Received deposit exception for Notification:{y} on Account:{x} - recording a problem status and skipping remaining actions for this account".format(x=acc.id, y=note.id))
                # record the failure against the status object
                limit = app.config.get("LONG_CYCLE_RETRY_LIMIT")
                status.record_failure(limit)
                status.save()
                return
    except client.JPERException as e:
        # save the status where we currently got to, so we can pick up again later
        status.save()
        app.logger.error(u"Problem while processing account for SWORD deposit: {x}".format(x=e.message))
        raise e

    # if we get to here, all the notifications for this account have been deposited, and we can update the status
    # and finish up
    status.save()
    app.logger.info("Leaving processing account")
    return


def process_notification(acc, note, since):
    """
    For the given account and notification, deliver the notification to the sword-enabled repository.

    The since date is required to check for duplication of notifications, this will avoid situations where the
    granularity of the since date and the last_deposit_date are too large and there are some processed and some unprocessed
    notifications all with the same timestamp

    :param acc: user account of repository
    :param note: notification to be deposited
    :param since: earliest date which the current set of requests is made from.
    """
    app.logger.info(u"Processing Notification:{y} for Account:{x}".format(x=acc.id, y=note.id))

    # for type inspection...
    assert isinstance(acc, models.Account)
    assert isinstance(note, jmod.OutgoingNotification)

    # first thing is to check the note for proximity to the since date, and check whether we did them already
    # this will avoid situations where the granularity of the since date and the last_deposit_date are too large
    # and there are some processed and some unprocessed notifications all with the same timestamp
    if note.analysis_date == since:
        # this gets the most recent deposit record for this id pair
        dr = models.DepositRecord.pull_by_ids(note.id, acc.id)

        # was this a successful deposit?  if so, don't re-run
        if dr is not None and dr.was_successful():
            app.logger.debug(u"Notification:{y} for Account:{x} was previously deposited - skipping".format(x=acc.id, y=note.id))
            return

    # work out if there is a content object to be deposited
    # which means asking the note if there's a content link with a package format supported
    # by the repository
    link = None
    packaging = None
    for p in acc.packaging:
        link = note.get_package_link(p)
        if link is not None:
            packaging = p

    # make a deposit record object to record the events
    dr = models.DepositRecord()
    dr.repository = acc.id
    dr.notification = note.id
    dr.deposit_date = dates.now()
    dr.id = dr.makeid()

    # pre-populate the content and completed bits of the deposit record, if there is no package to be deposited
    if link is None:
        dr.content_status = "none"
        dr.completed_status = "none"

    # make the metadata deposit, determining whether to immediately complete the deposit if there is no link
    # for content
    try:
        receipt = metadata_deposit(note, acc, dr, complete=link is None)
        # ensure the metadata status is set as we expect it
        dr.metadata_status = "deposited"
    except DepositException as e:
        app.logger.error(u"Received metadata deposit exception for Notification:{y} on Account:{x} - recording a failed deposit and ceasing processing on this notification".format(x=acc.id, y=note.id))
        # save the actual deposit record, ensuring that the metadata_status is set the way we expect
        dr.metadata_status = "failed"
        if app.config.get("STORE_RESPONSE_DATA", False):
            dr.save()

        # kick the exception upstairs for continued handling
        raise e

    # beyond this point, we are only dealing with content, so if there's no content to deposit we can
    # wrap up and return
    if link is None:
        app.logger.debug(u"No content files to deposit for Notification:{y} on Account:{x}".format(x=acc.id, y=note.id))
        if app.config.get("STORE_RESPONSE_DATA", False):
            dr.save()
        return

    # if we get to here, we have to deal with the content deposit

    # first, get a copy of the file from the API into the local tmp store
    local_id, path = _cache_content(link, note, acc)

    # make a copy of the tmp store for removing the content later
    tmp = store.StoreFactory.tmp()

    # now we can do the deposit from the locally stored file (which we need because we're going to use seek() on it
    # which we can't do with the http stream)
    with open(path, "rb") as f:
        try:
            package_deposit(receipt, f, packaging, acc, dr)
            # ensure the content status is set as we expect it
            dr.content_status = "deposited"
        except DepositException as e:
            app.logger.error(u"Received package deposit exception for Notification:{y} on Account:{x} - recording a failed deposit and ceasing processing on this notification".format(x=acc.id, y=note.id))
            # save the actual deposit record, ensuring the content_status is set the way we expect
            dr.content_status = "failed"
            if app.config.get("STORE_RESPONSE_DATA", False):
                dr.save()

            # delete the locally stored content
            tmp.delete(local_id)

            # kick the exception upstairs for continued handling
            raise e

    # now we can get rid of the locally stored content
    tmp.delete(local_id)

    # finally, complete the request
    try:
        complete_deposit(receipt, acc, dr)
        # ensure the completed status is set as we expect it
        dr.completed_status = "deposited"
    except DepositException as e:
        app.logger.error(u"Received complete request exception for Notification:{y} on Account:{x} - recording a failed deposit and ceasing processing on this notification".format(x=acc.id, y=note.id))

        # save the actual deposit record, ensuring the completed_status is set the way we expect
        dr.completed_status = "failed"
        if app.config.get("STORE_RESPONSE_DATA", False):
            dr.save()

        # kick the exception upstairs for continued handling
        raise e

    # that's it, we've successfully deposited this notification to the repository along with all its content
    if app.config.get("STORE_RESPONSE_DATA", False):
        dr.save()
    app.logger.info("Leaving processing notification")
    return

def _cache_content(link, note, acc):
    """
    Make a local copy of the content referenced by the link

    This will copy the content retrieved via the link into the temp store for use in the onward relay

    :param link: url to content
    :param note: notification we are working on
    :param acc: user account we are working as
    """
    app.logger.info("Entering _cache_content")
    j = client.JPER(api_key=acc.api_key)
    try:
        gen, headers = j.get_content(link.get("url"))
    except client.JPERException as e:
        app.logger.error(u"Problem while processing notification for SWORD deposit: {x}".format(x=e.message))
        raise e

    local_id = uuid.uuid4().hex
    tmp = store.StoreFactory.tmp()
    tmp.store(local_id, "README.txt", source_stream=StringIO(note.id))
    fn = link.get("url").split("/")[-1]
    out = tmp.path(local_id, fn, must_exist=False)

    with open(out, "wb") as f:
        for chunk in gen:
            if chunk:
                f.write(chunk)

    app.logger.info("Leaving _cache_content")
    return local_id, out

def metadata_deposit(note, acc, deposit_record, complete=False):
    """
    Deposit the metadata from the notification in the target repository

    :param note: the notification to be deposited
    :param acc: the account we are working as
    :param deposit_record: provenance object for recording actions during this deposit process
    :param complete: True/False; should we tell the repository that the deposit process is complete (do this if there is no binary deposit to follow)
    :return: the deposit receipt from the sword client
    """
    app.logger.info(u"Depositing metadata for Notification:{y} for Account:{x}".format(x=acc.id, y=note.id))

    # create a connection object
    conn = sword2.Connection(user_name=acc.sword_username, user_pass=acc.sword_password, error_response_raises_exceptions=False, http_impl=client_http.OctopusHttpLayer())

    # storage manager instance for use later
    sm = store.StoreFactory.get()

    # assemble the atom entry for deposit
    entry = sword2.Entry()
    xwalk.to_dc_rioxx(note, entry)

    # do the deposit
    ip = not complete
    if acc.repository_software in ["eprints"]:
        # because EPrints doesn't allow "complete" requests, we leave everything in_progress for the purposes of consistency
        ip = True

    try:
        receipt = conn.create(col_iri=acc.sword_collection, metadata_entry=entry, in_progress=ip)
    except Exception as e:
        msg = u"Received Error:{a} attempting to create object in repository for Notification:{y} for Account:{x} - raising DepositException".format(a=e.message, y=note.id, x=acc.id)
        app.logger.error(msg)
        raise DepositException(msg)
    
    # if the receipt has a dom object, store it (it may be a deposit receipt or an error)
    if receipt.dom is not None and app.config.get("STORE_RESPONSE_DATA", False):
        content = receipt.to_xml()
        sm.store(deposit_record.id, "metadata_deposit_response.xml", source_stream=StringIO(content))

    # find out if this was an error document, and throw an error if so
    # (recording deposited/failed on the deposit_record along the way)
    if isinstance(receipt, sword2.Error_Document):
        deposit_record.metadata_status = "failed"
        msg = "Metadata deposit failed with status {x}".format(x=receipt.code)
        if app.config.get("STORE_RESPONSE_DATA", False):
            sm.store(deposit_record.id, "metadata_deposit.txt", source_stream=StringIO(msg))
        app.logger.debug(u"Received error document depositing metadata for Notification:{y} for Account:{x} - raising DepositException".format(x=acc.id, y=note.id))
        raise DepositException(msg)
    else:
        if app.config.get("STORE_RESPONSE_DATA", False):
            msg = "Metadata deposit was successful"
            sm.store(deposit_record.id, "metadata_deposit.txt", source_stream=StringIO(msg))
        deposit_record.metadata_status = "deposited"
        app.logger.debug(u"Metadata successfully deposited for Notification:{y} for Account:{x}".format(x=acc.id, y=note.id))

    # if this wasn't an error document, then we have a legitimate response, but we need the deposit receipt
    # so get it explicitly, and store it
    if receipt.dom is None:
        try:
            receipt = conn.get_deposit_receipt(receipt.edit)
        except Exception as e:
            msg = u"Received Error:{a} attempting to retrieve deposit receipt in repository for Notification:{y} for Account:{x} - raising DepositException".format(a=e.message, y=note.id, x=acc.id)
            app.logger.error(msg)
            raise DepositException(msg)
        if app.config.get("STORE_RESPONSE_DATA", False):
            content = receipt.to_xml()
            sm.store(deposit_record.id, "metadata_deposit_response.xml", source_stream=StringIO(content))

    # if this is an eprints repository, also send the XML as a file
    if acc.repository_software in ["eprints"]:
        xmlhandle = StringIO(str(entry))
        try:
            conn.add_file_to_resource(receipt.edit_media, xmlhandle, "sword.xml", "text/xml")
        except Exception as e:
            msg = u"Received Error:{a} attempting to deposit atom entry as file in repository for Notification:{y} for Account:{x} - raising DepositException".format(a=e.message, y=note.id, x=acc.id)
            app.logger.error(msg)
            raise DepositException(msg)

    app.logger.info("Leaving metadata deposit")
    return receipt

def package_deposit(receipt, file_handle, packaging, acc, deposit_record):
    """
    Deposit the binary package content to the target repository

    :param receipt: deposit receipt from the metadata deposit
    :param file_handle: the file handle on the binary content to deliver
    :param packaging: the package format identifier
    :param acc: the account we are working as
    :param deposit_record: provenance object for recording actions during this deposit process
    """
    app.logger.info(u"Depositing Package Format:{y} for Account:{x}".format(x=acc.id, y=packaging))

    # create a connection object
    conn = sword2.Connection(user_name=acc.sword_username, user_pass=acc.sword_password, error_response_raises_exceptions=False, http_impl=client_http.OctopusHttpLayer())

    # FIXME: not that neat, but eprints has special behaviours that we need to accommodate.  So, in the eprints
    # case we add the package as a file to the resource, but in all other cases we append the files to the
    # item
    if acc.repository_software in ["eprints"]:
        # this one adds the package as a new file to the item
        try:
            ur = conn.add_file_to_resource(receipt.edit_media, file_handle, "deposit.zip", "application/zip", packaging)
        except Exception as e:
            msg = u"Received Error:{a} attempting to deposit file in repository for Account:{x} - raising DepositException".format(a=e.message, x=acc.id)
            app.logger.error(msg)
            raise DepositException(msg)
    else:
        # this one would replace all the binary files
        try:
            ur = conn.update_files_for_resource(file_handle, "deposit.zip", mimetype="application/zip", packaging=packaging, dr=receipt)
        except Exception as e:
            msg = u"Received Error:{a} attempting to deposit file in repository for Account:{x} - raising DepositException".format(a=e.message, x=acc.id)
            app.logger.error(msg)
            raise DepositException(msg)

        # this one would append the package's files to the resource
        # ur = conn.append(payload=file_handle, filename="deposit.zip", mimetype="application/zip", packaging=packaging, dr=receipt)

    # storage manager instance
    sm = store.StoreFactory.get()

    # find out if this was an error document, and throw an error if so
    # (recording deposited/failed on the deposit_record along the way)
    if isinstance(ur, sword2.Error_Document):
        deposit_record.content_status = "failed"
        msg = "Content deposit failed with status {x}".format(x=ur.code)
        if app.config.get("STORE_RESPONSE_DATA", False):
            sm.store(deposit_record.id, "content_deposit.txt", source_stream=StringIO(msg))
        app.logger.debug(u"Received error document depositing Package Format:{y} for Account:{x} - raising DepositException".format(x=acc.id, y=packaging))
        raise DepositException(msg)
    else:
        if app.config.get("STORE_RESPONSE_DATA", False):
            msg = "Content deposit was successful"
            sm.store(deposit_record.id, "content_deposit.txt", source_stream=StringIO(msg))
        deposit_record.content_status = "deposited"
        app.logger.debug(u"Successfully deposited Package Format:{y} for Account:{x}".format(x=acc.id, y=packaging))

    app.logger.info("Package deposit")
    return


def complete_deposit(receipt, acc, deposit_record):
    """
    Issue a "complete" request against the repository, to indicate that no further files are coming

    :param receipt: deposit receipt from previous metadata deposit
    :param acc: account we are working as
    :param deposit_record: provenance object for recording actions during this deposit process
    """
    app.logger.info(u"Sending complete request for Account:{x}".format(x=acc.id))

    # EPrints repositories can't handle the "complete" request
    cr = None
    if acc.repository_software not in ["eprints"]:
        # create a connection object
        conn = sword2.Connection(user_name=acc.sword_username, user_pass=acc.sword_password, error_response_raises_exceptions=False, http_impl=client_http.OctopusHttpLayer())

        # send the complete request to the repository
        try:
            cr = conn.complete_deposit(dr=receipt)
        except Exception as e:
            msg = u"Received Error:{a} attempting to complete deposit in repository for Account:{x} - raising DepositException".format(a=e.message, x=acc.id)
            app.logger.error(msg)
            raise DepositException(msg)

    # storage manager instance
    sm = store.StoreFactory.get()

    # find out if this was an error document, and throw an error if so
    # (recording deposited/failed on the deposit_record along the way)
    if cr is None:
        deposit_record.completed_status = "none"
        if app.config.get("STORE_RESPONSE_DATA", False):
            msg = "Complete request ignored, as repository is '{x}' which does not support this operation".format(x=acc.repository_software)
            sm.store(deposit_record.id, "complete_deposit.txt", source_stream=StringIO(msg))
    elif isinstance(cr, sword2.Error_Document):
        deposit_record.completed_status = "failed"
        msg = "Complete request failed with status {x}".format(x=cr.code)
        if app.config.get("STORE_RESPONSE_DATA", False):
            sm.store(deposit_record.id, "complete_deposit.txt", source_stream=StringIO(msg))
        app.logger.debug(u"Received error document for complete request for Account:{x}".format(x=acc.id))
        raise DepositException(msg)
    else:
        if app.config.get("STORE_RESPONSE_DATA", False):
            msg = "Complete request was successful"
            sm.store(deposit_record.id, "complete_deposit.txt", source_stream=StringIO(msg))
        deposit_record.completed_status = "deposited"
        app.logger.debug(u"Successfully sent complete request for Account:{x}".format(x=acc.id))

    app.logger.info("Leaving complete deposit")
    return

