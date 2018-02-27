
import os
import time
import datetime as dt
import json
import sys
from instrumenting.instrumenting import Timer
from gedcom import gedcom
from soundexpy import soundex
from .models import *
from .database import session, engine
from .helper import *
from sqlalchemy import and_

BATCH_SIZE = 100000


# todo: use the gedcom.py implementation instead
def get_chain(root, chain):
    tag = root
    for key in chain.split("."):
        if key in ["level", "xref", "tag", "value", "children", "parent"]:
            tag = getattr(tag, key)
        elif key == key.lower():
            tag = tag.additional.get(key, None)
        else:
            tag = tag.first_tag(key)
            if not tag:
                return None
    return ensure_unicode(tag)

def populate_from_gedcom(fname, store_gedcom=False):
    t0 = time.time()
    root = gedcom.read_file(fname)
    t1 = time.time()
    print "gedcom parsed     {}ms".format(str(int((t1 - t0)*1000)).rjust(8))
    for entry in root.traverse():
        if entry.tag == "FAM":
            if entry.level != 0:
                continue
            candidate = Family.query.filter_by(xref = ensure_unicode(entry.xref)).first()
            if candidate:
                print "Family '{}' already exists".format(entry.xref)
                continue
            fam = Family(
                    xref = ensure_unicode(entry.xref),
                    tag = u"FAM",
                    loaded_gedcom = ensure_unicode(gedcom.reform(entry)) if store_gedcom else None,
                    )
            session.add(fam)
    session.flush()
    t2 = time.time()
    print "families added    {}ms".format(str(int((t2 - t1)*1000)).rjust(8))
    for entry in root.traverse():
        if entry.tag == "INDI":
            if entry.level != 0:
                continue
            candidate = Individual.query.filter_by(xref = ensure_unicode(entry.xref)).first()
            if candidate:
                print "Individual '{}' already exists".format(entry.xref)
                continue
            names = get_chain(entry, "NAME.value").split("/")
            name_first = names[0].strip()
            name_family = names[1].strip()
            ind = Individual(
                    xref = ensure_unicode(entry.xref),
                    name = get_chain(entry, "NAME.value"),
                    name_first = name_first,
                    name_family = name_family,
                    tag = u"INDI",
                    sex = get_chain(entry, "SEX.value"),
                    birth_date_string = get_chain(entry, "BIRT.DATE.value"),
                    birth_date_year = get_chain(entry, "BIRT.DATE.year"),
                    # birth_date
                    death_date_string = get_chain(entry, "DEAT.DATE.value"),
                    death_date_year = get_chain(entry, "DEAT.DATE.year"),
                    # death_date
                    # soundex encodings
                    soundex_first = u(soundex.soundex(name_first.upper())),
                    soundex_family = u(soundex.soundex(name_family.upper())),
                    # loaded gedcom
                    loaded_gedcom = ensure_unicode(gedcom.reform(entry)) if store_gedcom else None,
                    )
            for tag in entry.by_tag("FAMC"):
                fam = Family.query.filter_by(xref = ensure_unicode(tag.value)).first()
                if not fam:
                    print "Family '{}' not found for individual '{}'".format(tag.xref, xref)
                    continue
                fam.children.append(ind)
            for tag in entry.by_tag("FAMS"):
                fam = Family.query.filter_by(xref = ensure_unicode(tag.value)).first()
                if not fam:
                    print "Family '{}' not found for individual '{}'".format(tag.xref, xref)
                    continue
                fam.parents.append(ind)
            session.add(ind)
    t3 = time.time()
    print "individuals added {}ms".format(str(int((t3 - t2)*1000)).rjust(8))
    if root.get_chain("HEAD.ROLE.value") == "test":
        testnote = ensure_unicode(root.get_chain("HEAD.ROLE.NOTE.value"))
        if testnote:
            testnotesetting = Setting.query.filter_by(key = "testnote").first()
            if testnotesetting:
                testnotesetting.value = testnote
            else:
                testnotesetting = Setting(key = "testnote", value = testnote)
                session.add(testnotesetting)
        print "testnote set"
    session.commit()

def reform_gedcom():
    def update(entry, chain, value):
        if value == None:
            return
        entry.edit_chain(chain, value)
    for ind in Individual.query.all():
        nextid = 123
        if ind.loaded_gedcom:
            entry = gedcom.read_string(ind.loaded_gedcom)
        else:
            entry = gedcom.Entry(0, "@I{}@".format(nextid), "INDI", None)
        update(entry, "NAME.value", ind.name)
        update(entry, "SEX.value", ind.sex)
        update(entry, "BIRT.DATE.value", ind.birth_date_string)
        update(entry, "DEAT.DATE.value", ind.death_date_string)
        for fam in ind.sub_families:
            print fam
        ind.loaded_gedcom = gedcom.reform(entry)
    for fam in Family.query.all():
        nextid = 123
        if fam.loaded_gedcom:
            entry = gedcom.read_string(fam.loaded_gedcom)
        else:
            entry = gedcom.Entry(0, "@F{}@".format(nextid), "FAM", None)


def yield_data_dicts(path, batch_idx=None, num_batches=None):
    with open(path) as f:
        if path.endswith('rows.json'):
            for i, line in enumerate(f):
                #if i == 20000:
                #    break
                if batch_idx is not None and num_batches is not None and i % num_batches != batch_idx:
                    continue
                yield json.loads(line)
        else:
            data = json.load(f)
            for d in data:
                yield d


def to_dict(**kwargs):
    return kwargs

def populate_from_recons(fname, batch_idx=None, num_batches=None,
                         do_pre_dict=True):
    t = Timer(True, 48)
    base = os.path.dirname(fname)
    f = open(fname)
    lines = f.readlines()
    f.close()
    sources = {}
    for line in lines:
        source, sourcefile = [x.strip() for x in line.split(":")]
        sources[source] = os.path.join(base, sourcefile)
    t.measure("header processed")
    count_parishes = None
    count_villages = None
    count_individuals = None
    count_families = None
    if "parishes" in sources:
        with open(sources["parishes"]) as f:
            data = json.load(f)
            for d in data:
                parish = Parish(**d)
                session.add(parish)
            count_parishes = len(data)
    t.measure("{} parishes added".format(count_parishes))
    if "villages" in sources:
        with open(sources["villages"]) as f:
            data = json.load(f)
            for d in data:
                d.pop("parish_name", None)
                village = Village(**d)
                session.add(village)
            count_villages = len(data)
    session.commit()
    t.measure("{} villages added".format(count_villages))
    celebrities = set()
    if "celebrities" in sources:
        with open(sources["celebrities"]) as fceleb:
            for line in fceleb:
                parts = line.split()
                if len(parts) > 0:
                    celeb_xref = u(parts[0].strip())
                    if not celeb_xref.startswith('#'):
                        celebrities.add(celeb_xref)
    t.measure("{} celebrities cached.".format(len(celebrities)))
    if "individuals" in sources:
        count_individuals = 0
        ind_inserts = []
        for didx, d in enumerate(yield_data_dicts(sources["individuals"], batch_idx=batch_idx,
                                                  num_batches=num_batches)):
            ind_inserts.append(to_dict(
                    xref = u(d["hiski_id"]),
                    name_first = u(d["first_name"]),
                    name_family = u(d["last_name"]),
                    name = u"{} {}".format(u(d["first_name"]), u(d["last_name"])).strip(),
                    normalized_name_first = u(d["normalized_first_name"]),
                    normalized_name_family = u(d["normalized_dad_last_name"]),
                    dad_first = u(d["dad_first_name"]),
                    dad_family = u(d["dad_last_name"]),
                    dad_patronym = u(d["dad_patronym"]),
                    normalized_dad_first = u(d["normalized_dad_first_name"]),
                    mom_first = u(d["mom_first_name"]),
                    mom_family = u(d["mom_last_name"]),
                    mom_patronym = u(d["mom_patronym"]),
                    normalized_mom_first = u(d["normalized_mom_first_name"]),
                    normalized_mom_family = u(d["normalized_mom_last_name"]),
                    tag = u"INDI",
                    sex = u"?",
                    is_celebrity = u(d["hiski_id"]) in celebrities or d.get("is_celebrity", False),
                    birth_date_string = u"{}.{}.{}".format(d["day"], d["month"], d["year"]),
                    birth_date_year = d["year"],
                    # todo: create a new column for burial id (now we're 
                    # using this string field which is otherwise unused)
                    death_date_string = d["burial_id"],
                    death_date_year = d["death_year"],
                    # todo: revise soundex storing to be more sensible
                    soundex_first = u(soundex.soundex(u(d["first_name"]).upper())),
                    soundex_family = u(soundex.soundex(u(d["last_name"]).upper())),
                    village_id = d["village_id"],
                    parish_id = d["parish_id"],
                    ))
            count_individuals += 1
            if didx % BATCH_SIZE == 0:
                print "\t{}\t(villages and parishes linked for {} individuals.)".format(
                        dt.datetime.now().isoformat()[:-7], didx)
                sys.stdout.flush()
                engine.execute(Individual.__table__.insert(), ind_inserts)
                ind_inserts = []
        engine.execute(Individual.__table__.insert(), ind_inserts)
        ind_inserts = []
        t.submeasure("individual objects created")
    xref2id = {}
    for id, xref in Individual.query.with_entities(Individual.id, Individual.xref).all():
        xref2id[xref] = id
    t.measure("{} individuals added".format(count_individuals))
    if "edges" in sources:
        edge_inserts = []
        prev_person = None
        for didx, d in enumerate(yield_data_dicts(sources["edges"], batch_idx=batch_idx,
                                                  num_batches=num_batches)):
            person_id = xref2id[u(d["child"])]
            is_selected = d.get('selected', person_id != prev_person)
            #pp = ParentProbability(
            edge_inserts.append(to_dict(
                    parent_id = xref2id[u(d["parent"])],
                    person_id = person_id,
                    probability = d["prob"],
                    is_dad = d["dad"],
                    is_selected = is_selected,
                    ))
            prev_person = person_id
            #session.add(pp)
            if didx % BATCH_SIZE == 0:
                print "\t{}\t({} edges processed.)".format(
                        dt.datetime.now().isoformat()[:-7], didx)
                sys.stdout.flush()
                #session.flush()
                engine.execute(ParentProbability.__table__.insert(), edge_inserts)
                edge_inserts = []
        engine.execute(ParentProbability.__table__.insert(), edge_inserts)
        edge_inserts = []
        t.submeasure("parent probabilities")
        selected_parents = {}
        for didx, d in enumerate(yield_data_dicts(sources["edges"], batch_idx=batch_idx,
                                                  num_batches=num_batches)):
            if d['selected']:
                if not d["child"] in selected_parents:
                    selected_parents[d["child"]] = []
                selected_parents[d['child']].append(d['parent'])
        t.submeasure("edges to parent_candidates")
        family_candidates = {}
        of_len = {}
        for child, parents in selected_parents.iteritems():
            key = tuple(parents)
            if not key in family_candidates:
                family_candidates[key] = []
            family_candidates[key].append(child)
            of_len[len(parents)] = of_len.get(len(parents), 0) + 1
        t.submeasure("parent_candidates to family_candidates")
        i = 0
        fam_inserts = []
        for parents, children in family_candidates.iteritems():
            i += 1
            fam_id = u"F{}".format(i)
            #fam = Family(
            fam_inserts.append(to_dict(
                    xref = fam_id,
                    tag = u"FAM",
                    ))
            #session.add(fam)
        #session.flush()
        engine.execute(Family.__table__.insert(), fam_inserts)
        del fam_inserts
        t.submeasure("families added")
        fam_xref2id = {}
        for id, xref in Family.query.with_entities(Family.id, Family.xref).all():
            fam_xref2id[xref] = id
        i = 0
        fp_inserts = []
        fc_inserts = []
        fp_set = set()
        fc_set = set()
        for parents, children in family_candidates.iteritems():
            i += 1
            fam_xref = u"F{}".format(i)
            fam_id = fam_xref2id[fam_xref]
            for parent in parents:
                #parent_link = FamilyParentLink(
                ind_id = xref2id[u(parent)]
                if (ind_id, fam_id) not in fp_set:
                    fp_inserts.append(to_dict(
                            individual_id = ind_id,
                            family_id = fam_id,
                            ))
                    fp_set.add((ind_id, fam_id))
                #session.add(parent_link)
                if len(fp_inserts) >= BATCH_SIZE:
                    engine.execute(FamilyParentLink.__table__.insert(), fp_inserts)
                    fp_inserts = []
            for child in children:
                #child_link = FamilyChildLink(
                ind_id = xref2id[u(child)]
                if (ind_id, fam_id) not in fc_set:
                    fc_inserts.append(to_dict(
                            individual_id = ind_id,
                            family_id = fam_id,
                            ))
                    fc_set.add((ind_id, fam_id))
                #session.add(child_link)
                if len(fc_inserts) >= BATCH_SIZE:
                    engine.execute(FamilyChildLink.__table__.insert(), fc_inserts)
                    fc_inserts = []
        if len(fp_inserts) > 0:
            engine.execute(FamilyParentLink.__table__.insert(), fp_inserts)
        if len(fc_inserts) > 0:
            engine.execute(FamilyChildLink.__table__.insert(), fc_inserts)
        del fp_inserts, fc_inserts, fp_set, fc_set
        t.submeasure("families linked to individuals")
        count_families = i
    t.measure("{} families added".format(count_families))
    if do_pre_dict:
        # Pre-compute dict representations for individuals and families.
        pre_dict()
        t.measure("pre-dicted individuals and families")
    session.commit()
    t.measure("commit")
    t.print_total()

def yield_batch_limits(n, batch_size=1000):
    if n <= batch_size:
        limits = [0, n+1]
    else:
        limits = range(1, n+2, batch_size)
    if limits[-1] <= n:
        limits.append(limits[-1] + batch_size)
    for i in range(len(limits) - 1):
        yield limits[i], limits[i+1]

def pre_dict():
    from sqlalchemy.sql.expression import bindparam

    pre_dict_batch = 10000000
    n_inds = session.query(Individual).count()
    print "Pre-dicting {} individuals.".format(n_inds)
    stmt = Individual.__table__.update().\
        where(Individual.id == bindparam('_id')).\
        values({
            'pre_dicted': bindparam('pre_dicted'),
        })
    pre_dicts = []
    for batch_i, (lo, hi) in enumerate(yield_batch_limits(n_inds, pre_dict_batch)):
        print dt.datetime.now().isoformat()[:-7], "Batch from {} to {}".format(lo, hi-1)
        sys.stdout.flush()
        t0 = time.time()
        if pre_dict_batch < n_inds:
            ind_query = Individual.query.filter(and_(Individual.id >= lo, Individual.id < hi))
#            ind_query = ind_query.options(joinedload(Individual.sup_families)).\
#                                  options(joinedload(Individual.sub_families)).\
#                                  options(joinedload(Individual.village)).\
#                                  options(joinedload(Individual.parish)).\
#                                  options(joinedload(Individual.parent_probabilities))#.\
#                                          joinedload(ParentProbability.person)).\
#                                  options(joinedload(Individual.parent_probabilities).
#                                          joinedload(ParentProbability.parent))
                                       #                ParentProbability.parent))
                             #options(joinedload(Individual.parent_probabilities).\
                             #             joinedload(person).\
                             #             joinedload(parent))
        else:
            ind_query = Individual.query
            ind_query = ind_query.options(joinedload(Individual.sup_families)).\
                                  options(joinedload(Individual.sub_families)).\
                                  options(joinedload(Individual.village)).\
                                  options(joinedload(Individual.parish)).\
                                  options(joinedload(Individual.parent_probabilities))
        inds = ind_query.all()
        print "  Querying {} individuals took {:.4f} seconds.".format(len(inds), time.time()-t0)
        sys.stdout.flush()
        t0 = time.time()
        for ii, ind in enumerate(inds):
            if pre_dict_batch >= n_inds and ii % 10000 == 0:
                print dt.datetime.now().isoformat()[:-7], ii
                sys.stdout.flush()
            pre_dicts.append({'pre_dicted': u(json.dumps(ind.as_dict(recompute=True))),
                              '_id': ind.id})
        print "  Pre-dicting took {:.4f} seconds.".format(time.time()-t0)
        if len(pre_dicts) > 0:
            t0 = time.time()
            engine.execute(stmt, pre_dicts)
            pre_dicts = []
            print "  Executing took {:.4f} seconds.".format(time.time()-t0)

    n_fams = session.query(Family).count()
    print "\nPre-dicting {} families.".format(n_fams)
    stmt = Family.__table__.update().\
        where(Family.id == bindparam('_id')).\
        values({
            'pre_dicted': bindparam('pre_dicted'),
        })
    pre_dicts = []
    for batch_i, (lo, hi) in enumerate(yield_batch_limits(n_fams, pre_dict_batch)):
        print dt.datetime.now().isoformat()[:-7], "Batch from {} to {}".format(lo, hi-1)
        sys.stdout.flush()
        fam_query = Family.query
        fam_query = fam_query.options(joinedload(Family.parents))\
                             .options(joinedload(Family.children))
        if n_fams > pre_dict_batch:
            fam_query = fam_query.filter(and_(Family.id >= lo, Family.id < hi))
        t0 = time.time()
        fams = fam_query.all()
        print "  Querying {} families took {:.4f} seconds.".format(len(fams), time.time()-t0)

        t0 = time.time()
        for ii, fam in enumerate(fam_query.all()):
            if pre_dict_batch >= n_fams and ii % 10000 == 0:
                print dt.datetime.now().isoformat()[:-7], ii
                sys.stdout.flush()
            pre_dicts.append({'pre_dicted': u(json.dumps(fam.as_dict())), '_id': fam.id})
        print "  Pre-dicting took {:.4f} seconds.".format(time.time()-t0)

        if len(pre_dicts) > 0:
            engine.execute(stmt, pre_dicts)
            pre_dicts = []

def update_deaths():
    from sqlalchemy.sql.expression import bindparam

    batch_size = 3000
    stmt = Individual.__table__.update().\
        where(Individual.id == bindparam('_id')).\
        values({
            'death_date_year': bindparam('death_date_year'),
            'death_date_string': bindparam('death_date_string'),
            'pre_dicted': bindparam('pre_dicted'),
        })
    updates = []
    f = open('recons_data/all_birth_burial_links.tsv')
    for i, line in enumerate(f):
        if i % 1000 == 0:
            print dt.datetime.now().isoformat()[:-7], i
        birth_id, burial_id, year = line.rstrip('\n').split('\t')
        year = int(year)
        birth_id = int(birth_id)
        burial_id = int(burial_id)
        ind = Individual.query.filter_by(xref = birth_id).first()
        pre_dict = json.loads(ind.pre_dicted)
        pre_dict['death_date_year'] = year
        pre_dict['death_date_string'] = burial_id
        updates.append({'_id': ind.id,
                        'death_date_year': year,
                        'death_date_string': burial_id,
                        'pre_dicted': u(json.dumps(pre_dict)),
                        })
        #pre_dict['death_date_year'] = None
        #pre_dict['death_date_string'] = None
        #updates.append({'_id': birth_id,
        #                'death_date_year': None,
        #                'death_date_string': None,
        #                'pre_dicted': u(json.dumps(pre_dict)),
        #                })
        if len(updates) > batch_size:
            t0 = time.time()
            engine.execute(stmt, updates)
            updates = []
            print "  Executing took {:.4f} seconds.".format(time.time()-t0)
            sys.stdout.flush()
    if len(updates) > 0:
        engine.execute(stmt, updates)
    session.commit()

def reset_deaths():
    from sqlalchemy.sql.expression import bindparam

    reset_batch = 10000
    n_inds = session.query(Individual).count()
    print "Resetting deaths for {} individuals.".format(n_inds)
    stmt = Individual.__table__.update().\
        where(Individual.id == bindparam('_id')).\
        values({
            'death_date_year': bindparam('death_date_year'),
            'death_date_string': bindparam('death_date_string'),
            'pre_dicted': bindparam('pre_dicted'),
        })
    updates = []
    for batch_i, (lo, hi) in enumerate(yield_batch_limits(n_inds, reset_batch)):
        #if lo < 2100001:
        #    continue
        print dt.datetime.now().isoformat()[:-7], "Batch from {} to {}".format(lo, hi-1)
        sys.stdout.flush()
        t0 = time.time()
        inds = Individual.query.filter(and_(Individual.id >= lo, Individual.id < hi)).all()
        print "  Querying {} individuals took {:.4f} seconds.".format(len(inds), time.time()-t0)
        sys.stdout.flush()
        t0 = time.time()
        for ii, ind in enumerate(inds):
            if ind.death_date_year is None:
                continue
            try:
                pre_dict = json.loads(ind.pre_dicted)
            except:
                print ind.id, ind.xref
                print "PD:", ind.pre_dicted
            pre_dict['death_date_year'] = None
            pre_dict['death_date_string'] = None
            updates.append({'_id': ind.id,
                            'death_date_year': None,
                            'death_date_string': None,
                            'pre_dicted': u(json.dumps(pre_dict)),
                            })
        print "  Pre-dicting took {:.4f} seconds.".format(time.time()-t0)
        if len(updates) > 0:
            t0 = time.time()
            engine.execute(stmt, updates)
            updates = []
            print "  Executing took {:.4f} seconds.".format(time.time()-t0)
            sys.stdout.flush()

def populate_component_ids():
    # NB: This is very slow and shouldn't be used for the full dataset!

    t = Timer(True, 60)
    # I'm not sure why the joinedload caused an exception, seemed like limit of
    # how much sqlite or sqlalchemy can retrieve from a query.
#    inds = Individual.query.options(joinedload("*")).all()
    inds = Individual.query.all()
    fams = Family.query.options().all()
#    dict_fams = {x.xref: x for x in fams}
    t.measure("queried to memory")
    for ind in inds:
        ind.component_id = 0
    t.measure("resetted component ids")
    next_id = 0
    max_size = 0
    for ind in inds:
        if ind.component_id > 0:
            continue
        next_id += 1
        buf = [ind]
        size = 0
        while buf:
            cur = buf.pop()
            if cur.component_id > 0:
                continue
            cur.component_id = next_id
            size += 1
            n_ids = []
            for fam in cur.sub_families + cur.sup_families:
                fam.component_id = next_id
                for ind2 in fam.parents + fam.children:
                    buf.append(ind2)
                    n_ids.append([fam.xref, ind2.xref])
            cur.neighboring_ids = u(json.dumps(n_ids))
#        t.submeasure("floodfill component {}".format(next_id))
        max_size = max(max_size, size)
    t.measure("floodfill {} components for {} people, max size {}".format(next_id, len(inds), max_size))
    session.commit()
    t.measure("commit")
    t.print_total()

def neighboring_ids():
    from sqlalchemy.sql.expression import bindparam

    batch = 1000
    n_inds = session.query(Individual).count()
    print "Computing neighboring ids for {} individuals.".format(n_inds)
    stmt = Individual.__table__.update().\
        where(Individual.id == bindparam('_id')).\
        values({
            'neighboring_ids': bindparam('neighboring_ids'),
        })
    updates = []
    for batch_i, (lo, hi) in enumerate(yield_batch_limits(n_inds, batch)):
        print dt.datetime.now().isoformat()[:-7], "Batch from {} to {}".format(lo, hi-1)
        sys.stdout.flush()
        t0 = time.time()
        if batch < n_inds:
            ind_query = Individual.query.filter(and_(Individual.id >= lo, Individual.id < hi))
            ind_query = ind_query.options(joinedload(Individual.sup_families)).\
                                  options(joinedload(Individual.sub_families))
        else:
            ind_query = Individual.query
            ind_query = ind_query.options(joinedload(Individual.sup_families)).\
                                  options(joinedload(Individual.sub_families))
        inds = ind_query.all()
        print "  Querying {} individuals took {:.4f} seconds.".format(len(inds), time.time()-t0)
        sys.stdout.flush()
        t0 = time.time()
        for ii, ind in enumerate(inds):
            n_ids = []
            for fam in ind.sub_families + ind.sup_families:
                for ind2 in fam.parents + fam.children:
                    n_ids.append([fam.xref, ind2.xref])
            updates.append({'neighboring_ids': u(json.dumps(n_ids)), '_id': ind.id})
        print "  Neighboring ids took {:.4f} seconds.".format(time.time()-t0)
        if len(updates) > 0:
            t0 = time.time()
            engine.execute(stmt, updates)
            updates = []
            print "  Executing took {:.4f} seconds.".format(time.time()-t0)

if __name__ == "__main__":
    pre_dict()
