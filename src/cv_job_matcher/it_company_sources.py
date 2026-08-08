from __future__ import annotations

import re

from .job_sources import JobProvider, SriLankaPortalProvider
from .models import CandidateProfile, Job
from .ats_sources import (
    BistecZohoCareerProvider,
    IfsCareerProvider,
    InforCareerProvider,
    ManatalCareerProvider,
    RootcodeApiProvider,
    SmartRecruitersProvider,
    SuccessFactorsRssProvider,
    TrakstarCareerProvider,
    ThreeCsCareerProvider,
)


# Official company-domain seeds. The bounded portal provider discovers a public
# careers/jobs link from each seed and applies the user's role-family gate.
# Keeping this registry in code makes additions reviewable and source coverage
# reproducible. A failed or empty site remains visible in source_coverage.csv.
SRI_LANKA_IT_COMPANY_CAREER_SEEDS: tuple[tuple[str, str], ...] = (
    ("WSO2", "https://wso2.com/careers/"),
    ("Virtusa Sri Lanka", "https://www.virtusa.com/careers"),
    ("IFS Sri Lanka", "https://www.ifs.com/careers"),
    ("Sysco LABS", "https://syscolabs.lk/careers/"),
    ("LSEG Sri Lanka", "https://www.lseg.com/en/careers"),
    ("99x", "https://99x.io/careers/"),
    ("CodeGen", "https://codegen.co.uk/careers/"),
    ("Creative Software", "https://www.creativesoftware.com/careers"),
    ("Zone24x7", "https://zone24x7.com/careers/"),
    ("Wiley Sri Lanka", "https://careers.wiley.com/"),
    ("Pearson Sri Lanka", "https://pearson.jobs/"),
    ("Axiata Digital Labs", "https://www.axiatadigitallabs.com/careers/"),
    ("MillenniumIT ESP", "https://www.mitesp.com/careers/"),
    ("Dialog Axiata", "https://www.dialog.lk/careers"),
    ("SLT-MOBITEL", "https://www.slt.lk/en/careers"),
    ("hSenid Software", "https://www.hsenid.com/careers/"),
    ("hSenid Business Solutions", "https://hsenidbiz.com/careers/"),
    ("Cambio Software Engineering", "https://www.cambio.se/career/"),
    ("Calcey Technologies", "https://calcey.com/careers/"),
    ("Surge Global", "https://surge.global/careers/"),
    ("Rootcode", "https://rootcode.io/careers"),
    ("Arimac", "https://arimaclanka.com/careers/"),
    ("IronOne Technologies", "https://www.irononetech.com/careers/"),
    ("DirectFN", "https://www.directfn.com/careers/"),
    ("Epic Lanka", "https://www.epictechnology.lk/careers/"),
    ("LOLC Technologies", "https://www.lolctech.com/careers/"),
    ("John Keells IT", "https://www.johnkeellsit.com/careers/"),
    ("John Keells X", "https://johnkeellsx.com/"),
    ("OREL IT", "https://orelit.com/careers/"),
    ("Inivos", "https://www.inivossl.com/careers/"),
    ("SimCentric Technologies", "https://www.simct.com/careers/"),
    ("Kingslake", "https://kingslake.com/careers/"),
    ("Databox Technologies", "https://databoxtech.io/"),
    ("IQZ Systems", "https://iqzsystems.com/"),
    ("GUI Solutions Lanka", "https://www.guisrilanka.com/"),
    ("Pagero Sri Lanka", "https://www.pagero.com/careers"),
    ("Novacura Lanka", "https://www.novacura.com/career/"),
    ("Codelantic", "https://codelantic.com/careers/"),
    ("Axis Tech Lanka", "https://axistech.lk/"),
    ("The AI Team", "https://the-ai.team/"),
    ("Fortude", "https://fortude.co/careers/"),
    ("DMS Software Engineering", "https://www.dmsswe.com/careers/"),
    ("DMS Electronics", "https://dmselectronics.com/careers/"),
    ("Just In Time Group", "https://www.jithpl.com/careers/"),
    ("Tech One Global", "https://www.techoneglobal.com/careers/"),
    ("Microimage", "https://microimage.com/careers/"),
    ("Fidenz Technologies", "https://fidenz.com/careers/"),
    ("Gapstars", "https://gapstars.net/careers/"),
    ("Nagarro Sri Lanka", "https://www.nagarro.com/en/careers"),
    ("HCLTech Sri Lanka", "https://www.hcltech.com/careers"),
    ("Accenture Sri Lanka", "https://www.accenture.com/lk-en/careers"),
    ("EY GDS Sri Lanka", "https://careers.ey.com/"),
    ("KPMG Sri Lanka Technology", "https://kpmg.com/lk/en/home/careers.html"),
    ("Deloitte Sri Lanka Technology", "https://www2.deloitte.com/lk/en/careers.html"),
    ("PwC Sri Lanka Technology", "https://www.pwc.com/lk/en/careers.html"),
    ("Mitra Innovation", "https://mitrai.com/careers/"),
    ("Acumatica Sri Lanka", "https://www.acumatica.com/careers/"),
    ("Infor Sri Lanka", "https://www.infor.com/about/careers"),
    ("Oracle Sri Lanka", "https://www.oracle.com/careers/"),
    ("Microsoft Sri Lanka", "https://careers.microsoft.com/"),
    ("IBM Sri Lanka", "https://www.ibm.com/careers"),
    ("Huawei Sri Lanka", "https://career.huawei.com/"),
    ("IFS Ultimo", "https://www.ultimo.com/careers/"),
    ("Bistec Global", "https://bistecglobal.com/careers/"),
    ("Eight25Media", "https://www.eight25media.com/careers/"),
    ("Elegant Media", "https://www.elegantmedia.com.au/careers/"),
    ("Antyra Solutions", "https://www.antyrasolutions.com/careers/"),
    ("3CS", "https://www.3cs.lk/careers/"),
    ("Weblook International", "https://weblook.com/careers/"),
    ("LakMobile", "https://lakmobile.com/careers/"),
    ("Omobio", "https://www.omobio.net/careers/"),
    ("NCINGA", "https://www.ncinga.net/careers/"),
    ("Enactor", "https://enactor.co/careers/"),
    ("Embla Software Innovation", "https://embla.as/careers/"),
    ("Navantis Sri Lanka", "https://www.navantis.com/careers/"),
    ("Insighture", "https://insighture.com/careers/"),
    ("Code94 Labs", "https://code94labs.com/careers/"),
    ("DigitalX", "https://digitalx.lk/careers/"),
    ("Xiteb", "https://www.xiteb.com/careers/"),
    ("Web Lankan", "https://www.weblankan.com/careers/"),
    ("eBEYONDS", "https://www.ebeyonds.com/careers/"),
    ("Cyber Concepts", "https://cyberconceptslk.com/careers/"),
    ("EchonLabs", "https://echonlabs.com/careers/"),
    ("SenzMate", "https://senzmate.com/careers/"),
    ("Linear Squared", "https://linearsquared.com/careers/"),
    ("PickMe Technology", "https://pickme.lk/careers"),
    ("Kapruka Technology", "https://www.kapruka.com/careers"),
    ("Daraz Sri Lanka Technology", "https://careers.daraz.com/"),
    ("ikman Technology", "https://ikman.lk/en/careers"),
    ("Roar Global Technology", "https://roar.global/careers/"),
    ("Bhasha Lanka", "https://www.bhasha.lk/careers/"),
    ("Ideamart", "https://www.ideamart.io/"),
    ("LankaPay Technology", "https://www.lankapay.net/careers/"),
    ("LankaClear", "https://www.lankaclear.com/careers/"),
    ("Softlogic Information Technologies", "https://www.softlogic.lk/careers"),
    ("Abans IT", "https://abansgroup.com/careers/"),
    ("Singer Digital Technology", "https://www.singersl.com/careers"),
    ("Blue Lotus 360", "https://www.bluelotus360.com/careers/"),
    ("CreativeHub Global", "https://creativehub.global/careers/"),
    ("Zeptolytics", "https://zeptolytics.com/careers/"),
)


# Independently opened career/vacancy endpoints supplied by the 2026-08-08
# verification pass. Only these endpoints are crawled. The other registry rows
# remain visible as skipped sources instead of wasting time on inferred URLs.
VERIFIED_IT_COMPANY_CAREER_URLS: dict[str, str] = {
    "WSO2": "https://wso2.com/careers",
    "99x": "https://99x.io/careers/open-positions?location=sri-lanka",
    "Virtusa Sri Lanka": "https://www.virtusa.com/careers",
    "IFS Sri Lanka": "https://www.ifs.com/en/about/careers?hl=en-GB",
    "Sysco LABS": "https://www.syscolabs.com/careers",
    "LSEG Sri Lanka": "https://www.lseg.com/en/careers/where-we-work/colombo-sri-lanka",
    "CodeGen": "https://codegen.co.uk/careers/",
    "Zone24x7": "https://zone24x7.com/careers/",
    "Wiley Sri Lanka": "https://www.wiley.com/en-us/about-us/careers/",
    "Pearson Sri Lanka": "https://pearson.hire.trakstar.com/",
    "Axiata Digital Labs": "https://www.axiatadigitallabs.com/adl-careers/",
    "MillenniumIT ESP": "https://www.careers-page.com/mitesp",
    "Dialog Axiata": "https://dialog.lk/careers?language=ta",
    "SLT-MOBITEL": "https://slt.lk/en/careers",
    "hSenid Business Solutions": "https://www.hsenidbiz.com/about-us/careers",
    "Cambio Software Engineering": "https://careers.cambio.lk/jobs",
    "Calcey Technologies": "https://calcey.com/careers/",
    "Surge Global": "https://surge.global/careers/",
    "Rootcode": "https://rootcode.ai/careers",
    "Epic Lanka": "https://careers.epictechnology.lk/vacancy",
    "John Keells IT": "https://careers.keells.com/JohnKeellsIT/go/Career-at-John-Keells-IT/516810",
    "John Keells X": "https://careers.keells.com/go/All-Jobs/516610/",
    "OREL IT": "https://careers.orelit.com/jobs",
    "IQZ Systems": "https://iqzsystems.com/careers",
    "GUI Solutions Lanka": "https://www.guisrilanka.com/career/",
    "Novacura Lanka": "https://job.novacura.com/jobs",
    "Axis Tech Lanka": "https://www.axis-tech.co/careers",
    "DMS Software Engineering": "https://dmsswe.com/careers/",
    "DMS Electronics": "https://www.dmslk.com/careers/",
    "Fortude": "https://fortude.co/our-people/",
    "Tech One Global": "https://techoneglobal.com/careers/",
    "Fidenz Technologies": "https://fidenz.com/careers/",
    "Gapstars": "https://gapstars.net/tech/jobs/",
    "Nagarro Sri Lanka": "https://www.nagarro.com/en/careers/sri-lanka",
    "HCLTech Sri Lanka": "https://www.hcltech.com/en-lk/careers/graduates",
    "EY GDS Sri Lanka": "https://careers.ey.com/",
    "KPMG Sri Lanka Technology": "https://kpmg.com/lk/en/careers.html",
    "PwC Sri Lanka Technology": "https://www.pwc.com/gx/en/careers.html",
    "Accenture Sri Lanka": "https://www.accenture.com/in-en/careers",
    "Deloitte Sri Lanka Technology": "https://southasiacareers.deloitte.com/go/Deloitte-Sri-Lanka-and-Maldives/718344/?q=&sortColumn=sort_title&sortDirection=desc",
    "Acumatica Sri Lanka": "https://careers.smartrecruiters.com/acumatica",
    "Infor Sri Lanka": "https://careers.infor.com/",
    "Oracle Sri Lanka": "https://www.oracle.com/careers/careers-at-oracle/",
    "IFS Ultimo": "https://careers.ultimo.com/",
    "Bistec Global": "https://bistecglobal.com/careers/",
    "Antyra Solutions": "https://www.antyrasolutions.com/about-us/careers/",
    "3CS": "https://www.3cs.lk/careers",
    "Weblook International": "https://weblook.com/careers-portal/",
    "Enactor": "https://enactor.co/careers/",
    "Insighture": "https://www.insighture.com/careers",
    "DigitalX": "https://digitalxlabs.com/careers/",
    "Xiteb": "https://careers.xiteb.com/public/vacancies",
    "eBEYONDS": "https://www.ebeyonds.com/careers/",
    "Cyber Concepts": "https://cyberconceptslk.com/careers/",
    "EchonLabs": "https://echonlabs.com/careers",
    "SenzMate": "https://www.senzmate.com/company/careers/join-our-journey/",
    "PickMe Technology": "https://pickme.lk/careers",
    "Kapruka Technology": "https://blog.kapruka.com/kapruka_careers/",
    "ikman Technology": "https://ikman.lk/en/shops/ikman-careers",
    "Roar Global Technology": "https://www.roar.global/careers",
    "LankaPay Technology": "https://lankapay.net/en/life-at-lankapay",
    "LankaClear": "https://lankapay.net/en/life-at-lankapay",
    "Ideamart": "https://hcmcloud.dialog.lk/CareerPortal/Careers?q=bEopnWmcv9llMiBG3zygOw%3D%3D",
    "Softlogic Information Technologies": "https://www.softlogic.lk/careers",
    "CreativeHub Global": "https://www.creativehub.global/join-our-team/",
}


class UnverifiedITCareerProvider(JobProvider):
    """Auditable placeholder for a company without a verified vacancy endpoint."""

    def __init__(self, company: str) -> None:
        self.name = f"{company} Careers"

    @property
    def disabled_reason(self) -> str:
        return "verified public career/vacancy endpoint not available"

    def search(
        self,
        profile: CandidateProfile,
        country: str,
        limit: int,
    ) -> list[Job]:
        return []


_IT_ROLE_TERMS = (
    "software", "developer", "programmer", "frontend", "front end",
    "backend", "back end", "full stack", "fullstack", "web engineer",
    "mobile developer", "android", "ios developer", "flutter", "react",
    "java developer", "python developer", "net developer", "devops",
    "site reliability", "sre", "cloud engineer", "cloud architect",
    "platform engineer", "data engineer", "data scientist", "data analyst",
    "database", "dba", "machine learning", "ml engineer", "ai engineer",
    "ai/ml", "artificial intelligence", "generative ai", "llm engineer",
    "nlp", "computer vision", "mlops", "cyber security", "cybersecurity",
    "information security", "security engineer", "network engineer",
    "systems engineer", "system administrator", "it support", "help desk",
    "qa engineer", "quality assurance", "test automation", "automation engineer",
    "solutions architect", "technical architect", "ui developer", "ux engineer",
    "business intelligence", "power bi", "sap consultant", "erp consultant",
    "technical lead", "engineering manager", "product engineer", "scrum master",
    "technical project manager", "information technology", "it manager",
)


def is_it_position(position: str) -> bool:
    normalized = " ".join(re.findall(r"[a-z0-9+#/.]+", position.casefold()))
    if not normalized:
        return False
    padded = f" {normalized} "
    return any(f" {term} " in padded or term in normalized for term in _IT_ROLE_TERMS)


def it_company_career_providers() -> list[JobProvider]:
    providers: list[JobProvider] = []
    for company, _seed_url in SRI_LANKA_IT_COMPANY_CAREER_SEEDS:
        verified_url = VERIFIED_IT_COMPANY_CAREER_URLS.get(company)
        if verified_url:
            name = f"{company} Careers"
            if company == "John Keells IT":
                provider = SuccessFactorsRssProvider(
                    name,
                    "https://careers.keells.com/services/rss/category/?catid=516810",
                    company,
                )
            elif company == "Deloitte Sri Lanka Technology":
                provider = SuccessFactorsRssProvider(
                    name,
                    "https://southasiacareers.deloitte.com/services/rss/category/?catid=718344",
                    "Deloitte Sri Lanka",
                )
            elif company == "Acumatica Sri Lanka":
                provider = SmartRecruitersProvider(name, "acumatica", "Acumatica")
            elif company == "MillenniumIT ESP":
                provider = ManatalCareerProvider(name, "mitesp", company)
            elif company == "Pearson Sri Lanka":
                provider = TrakstarCareerProvider(name, verified_url, company)
            elif company == "Rootcode":
                provider = RootcodeApiProvider(name)
            elif company == "IFS Sri Lanka":
                provider = IfsCareerProvider()
            elif company == "Infor Sri Lanka":
                provider = InforCareerProvider()
            elif company == "Bistec Global":
                provider = BistecZohoCareerProvider()
            elif company == "3CS":
                provider = ThreeCsCareerProvider()
            else:
                provider = SriLankaPortalProvider(name, verified_url)
            providers.append(provider)
        else:
            providers.append(UnverifiedITCareerProvider(company))
    return providers
