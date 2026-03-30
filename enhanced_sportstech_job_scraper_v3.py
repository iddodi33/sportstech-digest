import requests
from bs4 import BeautifulSoup
import pandas as pd
import time
from datetime import datetime
import random
import logging
import os
from urllib.parse import urljoin, urlparse
import json

from dotenv import load_dotenv
load_dotenv()

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    filename='job_scraper.log'
)

class SportsTechJobScraper:
    def __init__(self):
        self.jobs = []
        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5',
            'Accept-Encoding': 'gzip, deflate',
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1'
        }
        self.session = requests.Session()
        self.session.headers.update(self.headers)
        
        # Core Irish/top-tier sportstech companies — always included regardless of title
        self.core_sportstech_companies = [
            "Kitman Labs", "Output Sports", "Orreco", "STATSports", "Stats Perform",
            "TixServe", "Playermaker", "Wiistream", "Clubforce", "Xtremepush",
            "TrojanTrack", "Sports Impact Technologies", "Revelate Fitness",
            "Fanatics", "Flutter Entertainment", "FanDuel", "Betfair", "BoyleSports",
            "Teamworks", "Catapult", "Hudl", "Sportradar", "Genius Sports",
            "Second Spectrum", "PlayMetrics", "GameOn", "Performa Sports",
            "SportLoMo", "ClubZap", "Danu Sports", "SportsKey", "EquiRatings",
            "Glofox", "LegitFit", "Nutritics", "Web Summit", "WHOOP",
            "Sport Ireland", "IRFU", "FAI", "GAA", "Swim Ireland",
            "Athletics Ireland", "Basketball Ireland", "Leinster Rugby",
            "Munster Rugby", "Ulster Rugby", "Connacht Rugby",
            "Feenix Group", "Anyscor", "Locker", "Precision Sports Technology",
            "HEADHAWK", "TeamFeePay", "ClubSpot", "What's the Scór",
        ]

        # Companies to hard-exclude — consultancies, big tech, unrelated
        self.exclude_companies = [
            "Yahoo", "UPMC", "CarTrawler", "An Post", "Susquehanna",
            "TRACTIAN", "Spanish Point", "Irish Society of Chartered",
            "Accenture", "Deloitte", "PwC", "KPMG", "Ernst & Young",
            "Amazon Web", "Google", "Microsoft", "Apple Inc", "Meta Platforms", "IBM Ireland",
        ]

        # List of sports companies to search for
        self.sports_companies = [
            "ABC Fitness", "InStat Sport", "Hudl", "Covers", "STATSports", "Stats Perform", 
            "Nutritics", "Clubforce", "LegitFit", "Playertek", "Catapult", "Feenix Group", 
            "Epic Global", "Extratime", "Nativz Gaming", "Output Sports", "PlayON", "Tixserve", 
            "PLAYHERA", "GGCircuit", "RAID Studios", "ClubZap", "Kairos Sport", "Coras", 
            "Eventmaster", "Danu Sports", "Field of Vision", "SportsKey", "Usheru", 
            "Flutter Entertainment", "W4 Games", "VRAI", "Fantagoal", "Amazon Aws", "PwC", 
            "Deloitte", "Glofox", "Kitman Labs", "SportLoMo", "Tagmarshal", "WiiStream", 
            "LiveDuel", "CRAOI", "Fanmode", "EquiRatings", "Precision Sports Technology", 
            "KinetikIQ", "RugbySmarts", "Avenir Sports", "Rypt", "Brace", "RugbyPass", 
            "Volograms", "Gambling.com", "Tribes Studio", "What's The Scór", "Magic Media", 
            "GBE Technologies", "Motivation Weight Management", "FLYEfit", "Golfgraffix", 
            "ZYTO", "BleeperBike", "Elivar", "Fierce Fun", "Locker Sport", "Endorse", 
            "Clubber tv", "Hiiker", "Health and Sport Technologies", "Mitchell Dance Platform", 
            "Movement SAOL", "GYMIX", "iBreve", "Morning Line Club", "nTrai", "Playnbrag", 
            "TrojanTrack", "Occlusion Cuff", "Run Angel", "Incisiv", "GoChallenge", 
            "Sports Timing", "Mcgregor Fast", "Surfholidays.com", "MixRift", "Healing Hand Tech", 
            "Loudplay", "Betstone", "Giraffe Games", "Pff", "Food Choice at Work", "N-Pro", 
            "Wylde GG", "Balls.ie", "Mingo", "Score Beo", "Clubs4Hire", "ClubsToHire", 
            "Cubicle 7 Entertainment", "SuperNimbus", "RocksPRO", "Screen Scene Group", 
            "Impact Gumshields", "Ace Health Innovations", "Insulcheck", "Peri", "identifyHer", 
            "BetBright", "888", "Hamstring Solo", "MatchDay Technologies", "W1Da Experience", 
            "BragBet", "Runlastman.com", "Sportech 37", "TickerFit", "BigFan", "Cyc-Lok", 
            "CHAMPIONSID.COM", "FanFootage", "MeoCare", "FunkedUp", "Lumafit", "All Set Workplace", 
            "Adfaces", "Prospr", "BetDuel", "flexibod", "Profile 90", "EVB Sports Shorts", 
            "Ticket ABC", "TURAS Bikes", "Oddsfutures.com", "Yoga Teacher Assistant", "GolfBirdie", 
            "XTREEMO", "Fanaticus", "IlluminAi", "Red Moose", "Brim Brothers", "Golf Voyager", 
            "FantasyTote", "Clinics in Motion", "Amigo Media", "Odikyo", "Genetic Performance", 
            "Raceix", "Enzolve Technologies", "Kidzivity", "Simply Golf", "Surpass Sport Systems", 
            "Performance Tracking Solutions", "Global Institute of Physical Literacy", "ClubApp", 
            "Crowdsight", "Garbh Software Solutions", "ProActive Stats", "Relivvit", "FastForm", 
            "OOYO Sports", "Assesspatients", "Hole More Putts", "LifterDojo", "Beat Your Manager", 
            "Fantasy Games", "TotelFootball", "Isicall", "RigBag", "Bioscreen Health", "Gymr", 
            "Escape 2 Sport", "SportCurve", "Coachbook", "VouMove", "Body Project", "fitnessBattle", 
            "CogniGolf", "Champion's Mind", "ASX Sports", "Sula Health", "ND Sport Performance", 
            "SportCaller", "Sport Authority", "HavaBet", "Mobstats", "Huggity", 
            "Fast Fit Body Sculpting", "IsoFit", "Beta Dash", "myClub", "Hotfoot", "Sportora", 
            "Hodgson Moore Pathology Services", "BR Sensors", "ForFit", "TechniFit", "MyGAAClub", 
            "Comortais", "GAAther", "Book-E", "PocketCoach", "SmashQuiz", "Ace TeamTalk", 
            "Gaelcoach", "Outfitable", "R Club Sports Wear", "BikeBox", "Off The Ball", "Orreco", 
            "Performa Sports", "WHOOP", "Fanatics", "Flutter", "Opta",
            # Adding some major sports organizations in Ireland
            "GAA", "IRFU", "FAI", "Sport Ireland", "Sport Ireland Institute",
            "Basketball Ireland", "Hockey Ireland", "Swim Ireland", "Athletics Ireland", 
            "Cycling Ireland", "Gymnastics Ireland", "Triathlon Ireland", "Tennis Ireland"
        ]
        
        # Corporate career pages configuration
        # Note: Many corporate sites use JavaScript or have anti-scraping measures
        # LinkedIn searches often capture these jobs anyway
        self.corporate_sites = {
            'Flutter Entertainment': {
                'urls': [
                    'https://jobs.lever.co/flutter',  # More reliable Lever URL
                ],
                'location_filter': ['Dublin', 'Ireland', 'Remote'],
                'scraper_type': 'lever',
                'notes': 'Also check LinkedIn - posts most roles there'
            },
            'STATSports': {
                'urls': [
                    'https://apply.workable.com/statsports/'  # They use Workable ATS
                ],
                'location_filter': ['Ireland', 'Newry', 'Northern Ireland'],
                'scraper_type': 'workable',
                'notes': 'Northern Ireland based, often hires across Ireland'
            },
            'Stats Perform': {
                'urls': [
                    'https://eobe.fa.em2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1001/requisitions?mode=location'
                ],
                'location_filter': ['Ireland', 'Limerick', 'Dublin'],
                'scraper_type': 'oracle',
                'notes': 'Limerick HQ - Global Rugby Centre of Excellence, 300+ employees'
            },
            'WHOOP': {
                'urls': [
                    'https://jobs.lever.co/whoop'
                ],
                'location_filter': ['Ireland', 'Dublin', 'Cork', 'Galway', 'Limerick',
                                    'Belfast', 'Waterford', 'Remote', 'EMEA', 'Europe', 'UK'],
                'scraper_type': 'lever',
                'notes': 'US company but hires remote internationally; filter to Irish/EMEA roles only'
            },
        }
        
        # Companies better found via LinkedIn (JavaScript-heavy or restricted)
        self.linkedin_priority_companies = [
            'Fanatics',  # Oracle HCM - JavaScript heavy
            'Orreco',  # Custom site with SSL issues
            'Output Sports',  # Often posts to LinkedIn
            'Kitman Labs',  # LinkedIn preferred
        ]
        
    def scrape_linkedin(self, query="sports technology", location="Ireland", pages=5):
        """Scrape job listings from LinkedIn with robust timeout handling"""
        logging.info(f"Starting LinkedIn scrape for {query} in {location}")
        print(f"Starting LinkedIn scrape for '{query}' in {location}")
        
        linkedin_jobs_found = 0
        
        for page in range(pages):
            try:
                url = f"https://www.linkedin.com/jobs/search/?keywords={query.replace(' ', '%20')}&location={location.replace(' ', '%20')}&start={page*25}"
                print(f"  Accessing LinkedIn URL: {url}")
                
                response = self.session.get(url, timeout=20)
                
                print(f"  LinkedIn page {page+1} status code: {response.status_code}")
                
                if response.status_code != 200:
                    logging.error(f"Failed to fetch LinkedIn page {page+1}: Status code {response.status_code}")
                    print(f"  Error: Failed to fetch LinkedIn page {page+1}")
                    continue
                    
                soup = BeautifulSoup(response.text, 'html.parser')
                
                # LinkedIn may use different class names - try multiple options
                job_cards = soup.find_all('div', class_='base-card') or \
                           soup.find_all('div', class_='job-search-card') or \
                           soup.find_all('li', class_='jobs-search-results__list-item') or \
                           soup.find_all('div', class_='result-card') or \
                           soup.find_all('div', class_='jobs-search__results-list') or \
                           soup.find_all('li')
                
                print(f"  Found {len(job_cards)} potential job cards on LinkedIn page {page+1}")
                
                page_jobs_found = 0
                for job in job_cards:
                    try:
                        # Try multiple possible selectors for title
                        title_elem = job.find('h3', class_='base-search-card__title') or \
                                   job.find('h3', class_='job-search-card__title') or \
                                   job.find('a', class_='result-card__full-card-link') or \
                                   job.find('h3') or \
                                   job.find('h2')
                                   
                        # Try multiple possible selectors for company
                        company_elem = job.find('h4', class_='base-search-card__subtitle') or \
                                     job.find('h4', class_='job-search-card__subtitle') or \
                                     job.find('a', class_='job-search-card__subtitle-link') or \
                                     job.find('h4') or \
                                     job.find('a', class_='result-card__subtitle-link')
                                     
                        # Try multiple possible selectors for location
                        location_elem = job.find('span', class_='job-search-card__location') or \
                                      job.find('div', class_='job-search-card__location') or \
                                      job.find('span', class_='result-card__location')
                                      
                        # Try multiple possible selectors for link
                        link_elem = job.find('a', class_='base-card__full-link') or \
                                  job.find('a', class_='job-search-card__link') or \
                                  job.find('a', class_='result-card__full-card-link') or \
                                  job.find('a', href=True)
                        
                        # Extract text safely
                        title = ""
                        if title_elem:
                            title = title_elem.get_text(strip=True)
                        
                        company = ""
                        if company_elem:
                            company = company_elem.get_text(strip=True)
                        
                        location_text = "Ireland"
                        if location_elem:
                            location_text = location_elem.get_text(strip=True)
                        
                        link = ""
                        if link_elem and 'href' in link_elem.attrs:
                            link = link_elem['href']
                            if not link.startswith('http'):
                                link = urljoin('https://www.linkedin.com', link)
                        
                        # Only add job if we have at least a title
                        if title:
                            job_data = {
                                'title': title,
                                'company': company,
                                'location': location_text,
                                'link': link,
                                'summary': '',  # LinkedIn doesn't show summary in search results
                                'source': 'LinkedIn',
                                'scraped_date': datetime.now().strftime("%Y-%m-%d")
                            }
                            self.jobs.append(job_data)
                            page_jobs_found += 1
                            linkedin_jobs_found += 1
                    
                    except Exception as e:
                        logging.error(f"Error parsing LinkedIn job card: {e}")
                        continue
                
                print(f"  Extracted {page_jobs_found} jobs from LinkedIn page {page+1}")
                
                # Be respectful with rate limiting
                time.sleep(random.uniform(2, 4))
                
            except requests.exceptions.Timeout:
                logging.error(f"LinkedIn request timed out on page {page+1}")
                print(f"  Timeout error on LinkedIn page {page+1}")
                continue
            except Exception as e:
                logging.error(f"Error scraping LinkedIn page {page+1}: {e}")
                print(f"  Error on LinkedIn page {page+1}: {e}")
                continue
        
        print(f"Total jobs found on LinkedIn: {linkedin_jobs_found}")
        logging.info(f"LinkedIn scraping completed, found {linkedin_jobs_found} jobs")

    def scrape_lever_jobs(self, company_name, url, location_filter):
        """Scrape jobs from Lever-based career pages"""
        print(f"\nScraping {company_name} (Lever ATS)...")
        logging.info(f"Starting Lever scrape for {company_name}")
        
        try:
            # Try with session first
            response = self.session.get(url, timeout=30, verify=True)
            
            if response.status_code != 200:
                logging.error(f"Failed to fetch {company_name} Lever page: {response.status_code}")
                print(f"  Unable to access {company_name} careers page (Status: {response.status_code})")
                print(f"  Recommendation: Search LinkedIn for '{company_name} Ireland' instead")
                return
            
            soup = BeautifulSoup(response.text, 'html.parser')
            
            # Lever typically uses these classes - try multiple patterns
            job_postings = (
                soup.find_all('div', class_='posting') or 
                soup.find_all('a', class_='posting-title') or
                soup.find_all('div', {'data-qa': 'posting'}) or
                soup.find_all('a', href=lambda x: x and '/jobs/' in x)
            )
            
            if not job_postings:
                print(f"  No job listings found on {company_name} Lever page")
                print(f"  This could mean:")
                print(f"    - No current openings")
                print(f"    - Page structure changed")
                print(f"    - Jobs loaded via JavaScript")
                print(f"  Try: LinkedIn search for '{company_name} Ireland'")
                return
            
            jobs_found = 0
            
            for posting in job_postings:
                try:
                    title = ""
                    location = ""
                    link = ""
                    
                    # Extract title - try multiple selectors
                    title_elem = (
                        posting.find('h5') or 
                        posting.find('a', class_='posting-title') or
                        posting.find('h4') or
                        posting.find('div', class_='posting-title')
                    )
                    if title_elem:
                        title = title_elem.get_text(strip=True)
                    
                    # Extract location
                    location_elem = (
                        posting.find('span', class_='sort-by-location') or
                        posting.find('span', class_='location') or
                        posting.find('div', class_='posting-categories') or
                        posting.find('span', class_='posting-category')
                    )
                    if location_elem:
                        location = location_elem.get_text(strip=True)
                    
                    # Extract link
                    link_elem = posting if posting.name == 'a' else posting.find('a')
                    if link_elem and 'href' in link_elem.attrs:
                        link = link_elem['href']
                        if not link.startswith('http'):
                            link = urljoin(url, link)
                    
                    # Filter by location if specified
                    if location_filter and location:
                        location_match = any(loc.lower() in location.lower() for loc in location_filter)
                        if not location_match:
                            continue
                    
                    if title:
                        job_data = {
                            'title': title,
                            'company': company_name,
                            'location': location if location else 'Location TBD',
                            'link': link if link else url,
                            'summary': '',
                            'source': f'{company_name} Careers',
                            'scraped_date': datetime.now().strftime("%Y-%m-%d")
                        }
                        self.jobs.append(job_data)
                        jobs_found += 1
                
                except Exception as e:
                    logging.error(f"Error parsing {company_name} job: {e}")
                    continue
            
            if jobs_found > 0:
                print(f"  ✓ Found {jobs_found} jobs at {company_name}")
                logging.info(f"Scraped {jobs_found} jobs from {company_name}")
            else:
                print(f"  No Ireland-based jobs found (may have jobs in other locations)")
            
        except requests.exceptions.SSLError as e:
            logging.error(f"SSL Error scraping {company_name}: {e}")
            print(f"  SSL connection error with {company_name}")
            print(f"  Recommendation: Search LinkedIn for '{company_name} Ireland'")
        except requests.exceptions.Timeout:
            logging.error(f"Timeout scraping {company_name}")
            print(f"  Connection timeout with {company_name}")
            print(f"  Try: LinkedIn search for '{company_name} Ireland'")
        except Exception as e:
            logging.error(f"Error scraping {company_name}: {e}")
            print(f"  Error accessing {company_name}: {str(e)[:100]}")
            print(f"  Recommendation: Search LinkedIn for '{company_name} Ireland'")


    def scrape_workable_jobs(self, company_name, url, location_filter):
        """Scrape jobs from Workable-based career pages"""
        print(f"\nScraping {company_name} (Workable ATS)...")
        logging.info(f"Starting Workable scrape for {company_name}")
        
        try:
            response = self.session.get(url, timeout=30, verify=True)
            
            if response.status_code != 200:
                print(f"  Unable to access {company_name} Workable page")
                print(f"  Recommendation: Search LinkedIn for '{company_name} Ireland'")
                return
            
            soup = BeautifulSoup(response.text, 'html.parser')
            
            # Workable uses these common patterns
            job_postings = (
                soup.find_all('li', class_='job') or
                soup.find_all('li', class_='JobList__job') or
                soup.find_all('a', href=lambda x: x and '/jobs/' in x)
            )
            
            if not job_postings:
                print(f"  No current openings at {company_name}")
                print(f"  Check LinkedIn for '{company_name} Ireland' jobs")
                return
            
            jobs_found = 0
            
            for posting in job_postings:
                try:
                    # Extract title
                    title_elem = (
                        posting.find('h3') or
                        posting.find('h2') or
                        posting.find('a')
                    )
                    if not title_elem:
                        continue
                    
                    title = title_elem.get_text(strip=True)
                    
                    # Extract location
                    location_elem = posting.find('span', class_='location') or \
                                   posting.find('li', class_='location')
                    location = location_elem.get_text(strip=True) if location_elem else 'Northern Ireland'
                    
                    # Extract link
                    link_elem = posting if posting.name == 'a' else posting.find('a')
                    link = url
                    if link_elem and 'href' in link_elem.attrs:
                        link = link_elem['href']
                        if not link.startswith('http'):
                            link = urljoin(url, link)
                    
                    job_data = {
                        'title': title,
                        'company': company_name,
                        'location': location,
                        'link': link,
                        'summary': '',
                        'source': f'{company_name} Careers',
                        'scraped_date': datetime.now().strftime("%Y-%m-%d")
                    }
                    self.jobs.append(job_data)
                    jobs_found += 1
                
                except Exception as e:
                    continue
            
            if jobs_found > 0:
                print(f"  ✓ Found {jobs_found} jobs at {company_name}")
                logging.info(f"Scraped {jobs_found} jobs from {company_name}")
            
        except Exception as e:
            logging.error(f"Error scraping {company_name}: {e}")
            print(f"  Error accessing {company_name}")
            print(f"  Try: LinkedIn search for '{company_name} Ireland'")

    def scrape_oracle_jobs(self, company_name, url, location_filter):
        """Note about Oracle Cloud HCM career pages"""
        print(f"\nNote: {company_name} uses Oracle Cloud HCM (JavaScript-heavy)")
        print(f"  Direct scraping not reliable for this platform")
        print(f"  ✓ LinkedIn searches will capture these jobs")
        logging.info(f"Skipping {company_name} - Oracle HCM requires JavaScript rendering")

    def scrape_custom_career_page(self, company_name, url, location_filter):
        """Generic scraper for custom career pages"""
        print(f"\nScraping {company_name} (Custom Site)...")
        logging.info(f"Starting custom scrape for {company_name}")
        
        try:
            response = self.session.get(url, timeout=30, verify=True)
            
            if response.status_code != 200:
                print(f"  Unable to access {company_name} page")
                print(f"  Recommendation: LinkedIn search for '{company_name} Ireland'")
                return
            
            soup = BeautifulSoup(response.text, 'html.parser')
            
            # Try common job listing patterns
            job_containers = (
                soup.find_all('div', class_=lambda x: x and ('job' in x.lower() or 'position' in x.lower() or 'career' in x.lower())) or
                soup.find_all('li', class_=lambda x: x and ('job' in x.lower() or 'position' in x.lower())) or
                soup.find_all('article') or
                soup.find_all('a', href=lambda x: x and ('/jobs/' in x or '/careers/' in x or 'apply' in x.lower()))
            )
            
            jobs_found = 0
            
            for container in job_containers[:20]:  # Limit to avoid noise
                try:
                    # Try to extract job title
                    title_elem = (
                        container.find('h2') or 
                        container.find('h3') or 
                        container.find('h4') or
                        container.find('a')
                    )
                    
                    if not title_elem:
                        continue
                    
                    title = title_elem.get_text(strip=True)
                    
                    # Skip if title is too short or generic
                    if len(title) < 5 or title.lower() in ['apply', 'learn more', 'view job', 'careers', 'back']:
                        continue
                    
                    # Try to extract location
                    location = "Ireland"
                    location_elem = container.find('span', class_=lambda x: x and 'location' in x.lower()) or \
                                   container.find('p', class_=lambda x: x and 'location' in x.lower())
                    if location_elem:
                        location = location_elem.get_text(strip=True)
                    
                    # Try to extract link
                    link = url
                    link_elem = container if container.name == 'a' else container.find('a')
                    if link_elem and 'href' in link_elem.attrs:
                        link = link_elem['href']
                        if not link.startswith('http'):
                            link = urljoin(url, link)
                    
                    job_data = {
                        'title': title,
                        'company': company_name,
                        'location': location,
                        'link': link,
                        'summary': '',
                        'source': f'{company_name} Careers',
                        'scraped_date': datetime.now().strftime("%Y-%m-%d")
                    }
                    self.jobs.append(job_data)
                    jobs_found += 1
                
                except Exception as e:
                    continue
            
            if jobs_found == 0:
                print(f"  No job listings found on page")
                print(f"  Try: LinkedIn search for '{company_name} Ireland'")
            else:
                print(f"  ✓ Found {jobs_found} jobs at {company_name}")
                logging.info(f"Scraped {jobs_found} jobs from {company_name}")
            
        except Exception as e:
            logging.error(f"Error scraping {company_name}: {e}")
            print(f"  Error accessing {company_name}")
            print(f"  Recommendation: LinkedIn search for '{company_name} Ireland'")

    def scrape_corporate_sites(self):
        """Scrape all configured corporate career pages"""
        print("\n" + "="*60)
        print("SCRAPING CORPORATE CAREER PAGES")
        print("="*60)
        print("Note: Many companies use JavaScript-heavy sites.")
        print("LinkedIn searches will capture most of these jobs anyway.\n")
        
        for company_name, config in self.corporate_sites.items():
            for url in config['urls']:
                scraper_type = config['scraper_type']
                location_filter = config['location_filter']
                
                if scraper_type == 'lever':
                    self.scrape_lever_jobs(company_name, url, location_filter)
                elif scraper_type == 'workable':
                    self.scrape_workable_jobs(company_name, url, location_filter)
                elif scraper_type == 'oracle':
                    self.scrape_oracle_jobs(company_name, url, location_filter)
                elif scraper_type == 'custom':
                    self.scrape_custom_career_page(company_name, url, location_filter)
                
                # Be respectful with rate limiting
                time.sleep(random.uniform(2, 4))
        
        # Show note about LinkedIn-priority companies
        if self.linkedin_priority_companies:
            print(f"\n" + "-"*60)
            print("Additional companies best found via LinkedIn:")
            for company in self.linkedin_priority_companies:
                print(f"  • {company}")
            print("-"*60)
        
        print("\n" + "="*60)
        print("CORPORATE SCRAPING COMPLETED")
        print("="*60)

    def scrape_indeed_with_cloudscraper(self, query="sports technology", location="Ireland", max_results=50):
        """
        Experimental: Try to scrape Indeed using cloudscraper
        Note: Indeed has very strong Cloudflare protection
        This may or may not work depending on their current settings
        """
        print("\n" + "="*60)
        print("ATTEMPTING INDEED SCRAPING (EXPERIMENTAL)")
        print("="*60)
        print("⚠️  Note: Indeed uses aggressive anti-bot protection")
        print("⚠️  This may not work - LinkedIn is more reliable\n")
        
        try:
            # Try to import cloudscraper
            import cloudscraper
            
            print("  Creating cloudscraper instance...")
            scraper = cloudscraper.create_scraper(
                browser={
                    'browser': 'chrome',
                    'platform': 'windows',
                    'desktop': True
                },
                delay=10  # Add delay to avoid detection
            )
            
            # Build Indeed URL
            base_url = "https://ie.indeed.com/jobs"
            params = {
                'q': query,
                'l': location,
                'start': 0
            }
            
            jobs_found = 0
            page = 0
            indeed_page_jobs_raw = []

            while jobs_found < max_results and page < 3:  # Limit to 3 pages
                params['start'] = page * 10

                print(f"  Attempting Indeed page {page + 1}...")

                response = scraper.get(base_url, params=params, timeout=30)
                
                if response.status_code == 403:
                    print("  ❌ Indeed blocked the request (403 Forbidden)")
                    print("  → Cloudflare detected the scraper")
                    print("  → Recommendation: Use LinkedIn instead")
                    break
                elif response.status_code != 200:
                    print(f"  ❌ Indeed returned status code: {response.status_code}")
                    break
                
                soup = BeautifulSoup(response.text, 'html.parser')
                
                # Indeed uses these classes (as of 2024/2025)
                job_cards = soup.find_all('div', class_='job_seen_beacon') or \
                           soup.find_all('div', class_='cardOutline') or \
                           soup.find_all('a', class_='jcs-JobTitle')
                
                if not job_cards:
                    print("  ⚠️  No job cards found - page structure may have changed")
                    break
                
                page_jobs = 0
                for card in job_cards:
                    try:
                        # Extract title
                        title_elem = card.find('h2', class_='jobTitle') or \
                                    card.find('a', class_='jcs-JobTitle') or \
                                    card.find('span', {'title': True})
                        
                        if not title_elem:
                            continue
                        
                        title = title_elem.get_text(strip=True)
                        
                        # Extract company
                        company_elem = card.find('span', class_='companyName') or \
                                      card.find('span', {'data-testid': 'company-name'})
                        company = company_elem.get_text(strip=True) if company_elem else "Company Not Listed"
                        
                        # Extract location
                        location_elem = card.find('div', class_='companyLocation') or \
                                       card.find('div', {'data-testid': 'text-location'})
                        job_location = location_elem.get_text(strip=True) if location_elem else location
                        
                        # Extract link
                        link_elem = card.find('a', class_='jcs-JobTitle') or \
                                   title_elem if title_elem.name == 'a' else card.find('a')
                        
                        link = "https://ie.indeed.com"
                        if link_elem and 'href' in link_elem.attrs:
                            link = urljoin("https://ie.indeed.com", link_elem['href'])
                        
                        job_data = {
                            'title': title,
                            'company': company,
                            'location': job_location,
                            'link': link,
                            'summary': '',
                            'source': 'Indeed.ie',
                            'scraped_date': datetime.now().strftime("%Y-%m-%d")
                        }

                        indeed_page_jobs_raw.append(job_data)
                        jobs_found += 1
                        page_jobs += 1
                        
                    except Exception as e:
                        continue
                
                if page_jobs > 0:
                    print(f"  ✓ Found {page_jobs} jobs on Indeed page {page + 1}")
                
                page += 1
                time.sleep(random.uniform(5, 8))  # Longer delay between Indeed pages
            
            if jobs_found > 0:
                print(f"\n  ✓ Total Indeed jobs raw: {jobs_found}")
                filtered_indeed = self._apply_adzuna_indeed_filter("Indeed", indeed_page_jobs_raw)
                self.jobs.extend(filtered_indeed)
                logging.info(f"Scraped {jobs_found} Indeed jobs, kept {len(filtered_indeed)} after filter")
            else:
                print("\n  ℹ️  No jobs found on Indeed")
                print("  → This is normal - Indeed has strong anti-bot measures")
                print("  → LinkedIn will provide better coverage")
            
        except ImportError:
            print("  ⚠️  cloudscraper not installed")
            print("  → Install with: pip install cloudscraper --break-system-packages")
            print("  → OR just rely on LinkedIn (recommended)")
        except Exception as e:
            print(f"  ❌ Indeed scraping failed: {str(e)[:100]}")
            print("  → This is expected - Indeed blocks most scrapers")
            print("  → LinkedIn searches are more reliable")
            logging.error(f"Indeed scraping error: {e}")
        
        print("\n" + "="*60)
        print("INDEED SCRAPING COMPLETED")
        print("="*60)

    # ------------------------------------------------------------------
    # Location / company guard — applied to Adzuna and Indeed results
    # ------------------------------------------------------------------
    _EXCLUDE_LOCATIONS = [
        "boston", "new york", "london", "remote (us)",
        "united states", " us,", ", us ", "nyc",
    ]
    _INCLUDE_LOCATIONS = [
        "ireland", "dublin", "cork", "galway", "limerick",
        "belfast", "waterford", "remote",
    ]
    _SPORTSTECH_TITLE_KEYWORDS = [
        "sport", "athletic", "fitness", "stadium", "fan",
        "esport", "gaming", "performance", "wearable", "health tech",
    ]

    def _passes_adzuna_indeed_filter(self, job: dict) -> bool:
        """Return True if an Adzuna or Indeed job passes location + company/keyword checks."""
        loc = (job.get("location") or "").lower()

        # Hard exclude bad locations
        if any(excl in loc for excl in self._EXCLUDE_LOCATIONS):
            return False

        # Must be in an accepted location (or location unknown/empty)
        if loc and not any(incl in loc for incl in self._INCLUDE_LOCATIONS):
            return False

        # Company must match sports_companies list OR title must hit a keyword
        company = (job.get("company") or "").lower()
        title = (job.get("title") or "").lower()
        sports_companies_lower = [c.lower() for c in self.sports_companies]

        company_match = any(sc in company for sc in sports_companies_lower)
        keyword_match = any(kw in title for kw in self._SPORTSTECH_TITLE_KEYWORDS)

        return company_match or keyword_match

    def _apply_adzuna_indeed_filter(self, source_label: str, jobs: list[dict]) -> list[dict]:
        """Filter a batch of Adzuna/Indeed jobs and print a kept-vs-dropped summary."""
        kept, dropped = [], []
        for job in jobs:
            (kept if self._passes_adzuna_indeed_filter(job) else dropped).append(job)
        print(f"\n  [{source_label} filter] kept {len(kept)}, dropped {len(dropped)}")
        if dropped:
            for j in dropped[:5]:
                print(f"    dropped: {j['title']!r} @ {j.get('location', '?')!r} ({j.get('company', '?')!r})")
            if len(dropped) > 5:
                print(f"    … and {len(dropped) - 5} more")
        return kept

    def scrape_adzuna(self, query="sports technology", location="ireland", max_results=50):
        """Fetch jobs from the Adzuna API (requires ADZUNA_APP_ID + ADZUNA_APP_KEY in .env)."""
        app_id = os.getenv("ADZUNA_APP_ID")
        app_key = os.getenv("ADZUNA_APP_KEY")

        print("\n" + "="*60)
        print("SCRAPING ADZUNA API")
        print("="*60)

        if not app_id or app_id == "your_adzuna_app_id_here":
            print("  ⚠️  ADZUNA_APP_ID not set in .env — skipping Adzuna")
            return

        country = "ie"
        page = 1
        per_page = min(50, max_results)
        fetched = 0

        while fetched < max_results:
            url = f"https://api.adzuna.com/v1/api/jobs/{country}/search/{page}"
            params = {
                "app_id": app_id,
                "app_key": app_key,
                "results_per_page": per_page,
                "what": query,
                "where": location,
                "content-type": "application/json",
            }
            try:
                resp = self.session.get(url, params=params, timeout=15)
                resp.raise_for_status()
                data = resp.json()
            except Exception as exc:
                print(f"  ❌ Adzuna API error (page {page}): {exc}")
                logging.error(f"Adzuna error: {exc}")
                break

            results = data.get("results", [])
            if not results:
                break

            batch = []
            for r in results:
                batch.append({
                    "title": r.get("title", ""),
                    "company": r.get("company", {}).get("display_name", "Company Not Listed"),
                    "location": r.get("location", {}).get("display_name", ""),
                    "link": r.get("redirect_url", ""),
                    "summary": r.get("description", "")[:300],
                    "source": "Adzuna",
                    "scraped_date": datetime.now().strftime("%Y-%m-%d"),
                })

            batch = self._apply_adzuna_indeed_filter("Adzuna", batch)
            self.jobs.extend(batch)
            fetched += len(results)

            print(f"  ✓ Adzuna page {page}: {len(results)} raw → {len(batch)} kept")

            if len(results) < per_page:
                break
            page += 1
            time.sleep(random.uniform(1, 2))

        print("="*60)

    def filter_jobs(self):
        """Filter jobs based on sports technology relevance"""
        print(f"\nFiltering {len(self.jobs)} jobs for sports technology relevance...")
        
        # Keywords that indicate sports tech roles
        keywords = [
            'software', 'engineer', 'developer', 'data', 'analytics', 'product', 'design', 
            'technology', 'digital', 'tech', 'app', 'mobile', 'web', 'platform', 'cloud',
            'devops', 'backend', 'frontend', 'full stack', 'fullstack', 'machine learning',
            'ai', 'artificial intelligence', 'python', 'java', 'javascript', 'react', 
            'node', 'api', 'database', 'sql', 'aws', 'azure', 'infrastructure', 'security',
            'ux', 'ui', 'designer', 'product manager', 'scrum', 'agile', 'qa', 'testing',
            'performance', 'wearable', 'sports science', 'athlete', 'coaching', 'training',
            'fitness', 'health', 'wellness', 'biometric', 'sensor', 'tracking', 'gps'
        ]
        
        # Strong indicators that override exclusions
        strong_tech_indicators = [
            'software engineer', 'data scientist', 'product manager', 'tech lead',
            'engineering manager', 'developer', 'programmer', 'architect', 'devops',
            'machine learning', 'data engineer', 'backend', 'frontend', 'full stack'
        ]
        
        # Job types to exclude
        exclude_keywords = [
            'retail', 'sales associate', 'cashier', 'warehouse', 'driver', 'cleaner',
            'security guard', 'receptionist', 'admin assistant', 'customer service rep',
            'call center', 'barista', 'server', 'waiter', 'cook', 'chef', 'maintenance'
        ]

        # Non-sportstech title patterns — heavy penalty (Fix 3)
        non_sportstech_titles = [
            'physiotherapist', 'accountant', 'legal', 'solicitor', 'barrister',
            'cleaner', 'security guard', 'chef', 'driver', 'warehouse'
        ]

        core_lower    = [c.lower() for c in self.core_sportstech_companies]
        exclude_lower = [c.lower() for c in self.exclude_companies]
        sports_lower  = [c.lower() for c in self.sports_companies]

        filtered_jobs = []
        hard_excluded = 0

        for job in self.jobs:
            score = 0
            title   = job['title'].lower()
            summary = job['summary'].lower()
            company = job['company'].lower()

            # 0. Hard exclusion — unrelated companies
            if any(ex in company for ex in exclude_lower):
                hard_excluded += 1
                continue

            # 1. Core sportstech company → guaranteed pass (Fix 1)
            if any(c in company for c in core_lower):
                score += 8

            # 2. Strong tech indicators in title / summary
            if any(indicator.lower() in title for indicator in strong_tech_indicators):
                score += 10
            if any(indicator.lower() in summary for indicator in strong_tech_indicators):
                score += 5

            # 3. Tech keyword counts
            score += sum(1 for kw in keywords if kw.lower() in title) * 2
            score += sum(1 for kw in keywords if kw.lower() in summary)

            # 4. Penalty for excluded job types
            if any(keyword.lower() in title for keyword in exclude_keywords):
                score -= 5

            # 5. Penalty for clearly non-sportstech titles (Fix 3)
            if any(t in title for t in non_sportstech_titles):
                score -= 5

            # 6. Sports company match (reduced from 3 → 1 for non-core; Fix 1)
            if any(sc in company for sc in sports_lower) and not any(c in company for c in core_lower):
                score += 1

            # 7. Bonus for jobs from corporate sites (already pre-filtered)
            if job['source'] != 'LinkedIn':
                score += 5

            # 8. Filter based on final score
            if score > 0:
                if score >= 10:
                    job['relevancy'] = 'high'
                elif score >= 5:
                    job['relevancy'] = 'medium'
                else:
                    job['relevancy'] = 'low'
                filtered_jobs.append(job)
        
        self.jobs = filtered_jobs
        print(f"  Hard excluded {hard_excluded} jobs from non-sportstech companies")
        logging.info(f"Filtered to {len(self.jobs)} relevant sports technology jobs")
        print(f"Filtered to {len(self.jobs)} relevant sports technology jobs")
        
        # Additional logging of jobs by relevancy
        high_relevance = sum(1 for job in self.jobs if job.get('relevancy') == 'high')
        medium_relevance = sum(1 for job in self.jobs if job.get('relevancy') == 'medium')
        low_relevance = sum(1 for job in self.jobs if job.get('relevancy') == 'low')
        
        print(f"High relevance: {high_relevance}, Medium: {medium_relevance}, Low: {low_relevance}")
    
    def remove_duplicates(self):
        """Remove duplicate job listings based on title and company"""
        unique_jobs = []
        seen = set()
        
        for job in self.jobs:
            # Create a unique identifier for each job
            identifier = (job['title'].lower(), job['company'].lower())
            
            if identifier not in seen:
                seen.add(identifier)
                unique_jobs.append(job)
        
        self.jobs = unique_jobs
        print(f"After removing duplicates: {len(self.jobs)} unique jobs remaining")
        logging.info(f"Removed duplicates, {len(self.jobs)} unique jobs remaining")
    
    def save_to_csv(self, filename="sportstech_jobs_ireland.csv"):
        """Save job listings to a CSV file"""
        if not self.jobs:
            logging.warning("No jobs to save")
            print("No jobs found to save!")
            return
            
        df = pd.DataFrame(self.jobs)
        df['scrape_date'] = datetime.now().strftime("%Y-%m-%d")
        
        # Sort by source (corporate first), then relevancy
        source_order = df['source'].apply(lambda x: 0 if x != 'LinkedIn' else 1)
        df['source_order'] = source_order
        
        relevancy_order = {'high': 0, 'medium': 1, 'low': 2}
        df['relevancy_order'] = df['relevancy'].map(relevancy_order)
        
        df = df.sort_values(['source_order', 'relevancy_order'])
        df = df.drop(['source_order', 'relevancy_order'], axis=1)
        
        # Save to CSV
        df.to_csv(filename, index=False)
        logging.info(f"Saved {len(self.jobs)} jobs to {filename}")
        
        # Print summary
        print(f"\n{'='*60}")
        print("JOB SCRAPING SUMMARY")
        print(f"{'='*60}")
        
        # Summary by source
        print("\nJobs by Source:")
        source_counts = df['source'].value_counts().to_dict()
        for source, count in source_counts.items():
            print(f"  {source}: {count}")
        
        # Summary by relevancy
        print("\nJob Relevancy:")
        relevancy_counts = df['relevancy'].value_counts().to_dict()
        print(f"  High relevance: {relevancy_counts.get('high', 0)}")
        print(f"  Medium relevance: {relevancy_counts.get('medium', 0)}")
        print(f"  Low relevance: {relevancy_counts.get('low', 0)}")
        
        print(f"\nTotal unique jobs: {len(df)}")
        print(f"Results saved to: {filename}")
        print(f"{'='*60}")
        
        return filename


def run_enhanced_sportstech_scraper(enable_indeed=True):
    """Run the enhanced SportsTech job scraper
    
    Args:
        enable_indeed: If True, attempts to scrape Indeed (experimental, may not work)
    """
    print("\n" + "="*60)
    print("ENHANCED SPORTSTECH JOB SCRAPER FOR IRELAND")
    print("="*60)
    print(f"Started at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("="*60 + "\n")
    
    scraper = SportsTechJobScraper()
    
    # Step 1: Scrape corporate career pages
    scraper.scrape_corporate_sites()

    # Step 1.5: Adzuna API
    scraper.scrape_adzuna(query="sports technology", location="ireland", max_results=50)

    # Step 1.6: Optionally try Indeed (experimental)
    if enable_indeed:
        scraper.scrape_indeed_with_cloudscraper(
            query="sports technology",
            location="Ireland",
            max_results=50
        )
    
    # Step 2: Scrape LinkedIn
    print("\n" + "="*60)
    print("SCRAPING LINKEDIN")
    print("="*60)
    
    # Main sports tech searches
    print("\nRunning targeted sportstech searches...")
    scraper.scrape_linkedin(query="sports technology", location="Ireland", pages=5)
    
    # Additional focused searches
    tech_search_terms = [
        "sports analytics", 
        "sports data", 
        "sports software",
        "sports digital",
        "performance technology",
    ]
    
    print("\nRunning additional tech-specific searches...")
    for term in tech_search_terms:
        print(f"Searching for: {term}")
        scraper.scrape_linkedin(query=term, location="Ireland", pages=2)
        time.sleep(3)
    
    # Step 3: Process the scraped jobs
    print(f"\n{'='*60}")
    print(f"Total jobs collected: {len(scraper.jobs)}")
    print(f"{'='*60}")
    
    scraper.filter_jobs()
    scraper.remove_duplicates()
    
    # Step 4: Save to CSV
    csv_file = scraper.save_to_csv()
    
    logging.info(f"Enhanced SportsTech job scraper completed, data saved to {csv_file}")
    print(f"\n✓ Scraping completed successfully!")
    print(f"✓ Check '{csv_file}' for results")


# Run the scraper
if __name__ == "__main__":
    # Set enable_indeed=True to attempt Indeed scraping (may not work)
    # Set enable_indeed=False to skip Indeed (recommended - LinkedIn is more reliable)
    run_enhanced_sportstech_scraper(enable_indeed=False)
