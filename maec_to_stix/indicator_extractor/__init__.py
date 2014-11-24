import maec.utils
import maec_to_stix
from maec.package.package import Package
from maec.package.malware_subject import MalwareSubject
from maec.bundle.bundle import Bundle, ObjectList, ActionList
from maec.bundle.object_history import ObjectHistory
from maec.utils.deduplicator import BundleDeduplicator
import cybox.utils
from cybox.core import Object
from cybox.common import (ObjectProperties, ToolInformation, 
                          ToolInformation, ToolInformationList)
import stix.utils
from stix.core import STIXHeader, STIXPackage
from stix.common import Confidence, InformationSource
from stix.indicator import Indicator
from stix.ttp import TTP, Behavior
from stix.extensions.malware.maec_4_1_malware import MAECInstance
from indicator_filter import IndicatorFilter
from config_parser import ConfigParser

class IndicatorExtractor(object):
    def __init__(self, maec_package, file_name=None):
        # The input MAEC Package
        self.maec_package = maec_package
        # The output STIX Package (with Indicators)
        self.stix_package = None
        # The input file name
        self.file_name = file_name
        # Parsed configuration structure
        self.config = ConfigParser()
        # Set the STIX namespace and alias
        stix.utils.set_id_namespace({'https://github.com/MAECProject/maec-to-stix' : 'maecToSTIX'})
        # Set the MAEC namespace and alias
        maec.utils.set_id_namespace(cybox.utils.Namespace('https://github.com/MAECProject/maec-to-stix' , 'maecToSTIX'))
        # Parse the MAEC Package 
        self.parse_package()

    def add_stix_ttp(self, malware_subject):
        """Create and add a STIX TTP for a MAEC Malware Subject."""
        # Create the STIX TTP that includes the MAEC Instance
        ttp = TTP()
        ttp.behavior = Behavior()
        # Add a MAEC Package with just the Malware Subject
        # For capturing the identity of the malware binary that the Indicators target
        maec_package = Package()
        new_malware_subject = MalwareSubject()
        new_malware_subject.malware_instance_object_attributes = malware_subject.malware_instance_object_attributes
        maec_package.add_malware_subject(new_malware_subject)
        maec_malware_instance = MAECInstance()
        maec_malware_instance.maec = maec_package
        ttp.behavior.add_malware_instance(maec_malware_instance)
        self.stix_package.add_ttp(ttp)
        return ttp.id_

    def add_stix_indicators(self, final_indicator_objects, ttp_id):
        """Create and add STIX Indicators for a list of Object History entries.
           Link each Indicator to their Indicated TTP."""
        object_values_list = []
        actions_list = []
        final_object_list = []

        # Deduplicate the Objects and combine their Actions
        for entry in final_indicator_objects:
            object = entry.object
            # Test if we've already created an Indicator for this Object
            obj_values = BundleDeduplicator.get_object_values(object)
            if obj_values not in object_values_list:
                object_values_list.append(obj_values)
                final_object_list.append(object)
                actions_list.append(entry.get_action_names())
            else:
                object_index = object_values_list.index(obj_values)
                existing_actions = actions_list[object_index]
                existing_actions += entry.get_action_names()

        # Create the STIX Indicators
        for object in final_object_list:
            object_index = final_object_list.index(object)
            indicator = Indicator()
            indicator.title = "Malware Artifact Extracted from MAEC Document"
            indicator.add_indicator_type("Malware Artifacts")
            indicator.add_observable(object.properties)
            # Add the Action-derived description to the Indicator
            description = "Corresponding Action(s): "
            for action_name in actions_list[object_index]:
                description += (action_name + ", ")
            indicator.description = description[:-2]
            # Set the proper Confidence on the Indicator
            confidence = Confidence()
            confidence.value = "Low"
            confidence.description = "Tool-generated Indicator. It is HIGHLY recommended that it be vetted by a human analyst before usage."
            indicator.confidence = confidence
            # Link the Indicator to its Indicated TTP
            ttp = TTP(idref=ttp_id)
            indicator.add_indicated_ttp(ttp)
            # Add the Indicator to the STIX Package
            self.stix_package.add_indicator(indicator)
        
    def create_stix_package(self):
        """Create and return a STIX Package with the final Indicator Objects."""
        stix_package = STIXPackage()
        stix_header = STIXHeader()
        stix_header.add_package_intent("Indicators - Malware Artifacts")
        if self.file_name:
            stix_header.title = "STIX Indicators extracted from MAEC file: " + str(self.file_name)
        # Add the Information Source to the STIX Header
        tool_info = ToolInformation()
        stix_header.information_source = InformationSource()
        tool_info.name = "MAEC to STIX"
        tool_info.version = str(maec_to_stix.__version__)
        stix_header.information_source.tools = ToolInformationList(tool_info)
        stix_package.stix_header = stix_header
        return stix_package

    def set_object_property(self, property, condition = "Equals"):
        """Add a condition to an Object property and all of its children."""
        if isinstance(property, basestring) or hasattr(property, "__int__"):
            property = {'value':property, 'condition':condition}
        elif isinstance(property, dict):
            if 'condition' not in property and 'required' not in property:
                for key, value in property.items():
                    property[key] = self.set_object_property(value, condition)
        elif isinstance(property, list):
            for item in property:
                self.set_object_property(item, condition)
        return property

    def prepare_objects(self, final_indicator_objects):
        """Prepare the final Indicator Objects for translation into STIX Indicators.
           Set their condition attributes as appropriate."""
        for entry in final_indicator_objects:
            object = entry.object
            object_xsi_type = object.properties._XSI_TYPE
            object_properties_dict = object.properties.to_dict()
            updated_properties_dict = {}
            for property_name, property_value in object_properties_dict.items():
                updated_properties_dict[property_name] = self.set_object_property(property_value)
            updated_properties_dict['xsi:type'] = object_xsi_type
            object.properties = ObjectProperties.from_dict(updated_properties_dict)

    def parse_object_history(self, object_history):
        """Parse the Object History to build the list of
           candidate Objects for use in Indicators."""
        candidate_indicator_objects = []
        for entry in object_history.entries:
            object_id = entry.object.id_
            # Get the context with regards to the Actions that operated on the Object
            action_context = entry.get_action_context()
            action_match = False
            # First, test if one of the supported Actions operated on the Object
            for context_entry in action_context:
                if context_entry[0] in self.config.supported_actions:
                    action_match = True
                    break
            # If a supported Action was found, add the Object to the list of candidates
            if action_match:
                candidate_indicator_objects.append(entry)
        return candidate_indicator_objects

    def create_bundle_indicators(self, object_history, ttp_id):
        """Create an add Indicators derived from a MAEC Bundle."""
        # Parse the object history to build the list of candidate Objects
        candidate_indicator_objects = self.parse_object_history(object_history)
        # Instantiate the indicator filter
        indicator_filter = IndicatorFilter(self.config)
        # Prune the candidate objects
        pruned_indicator_objects = indicator_filter.prune_objects(candidate_indicator_objects)
        # Prepare the candidate objects for Indicatorization (TM)
        self.prepare_objects(pruned_indicator_objects)
        # Create and add the STIX Indicators for each of the final candidate indicator Objects
        self.add_stix_indicators(pruned_indicator_objects, ttp_id)

    def parse_bundle(self, bundle, ttp_id):
        """Parse a MAEC Bundle."""
        # Deduplicate the Bundle (for creating a full Object History for each Object)
        bundle.deduplicate()
        # Normalize the Objects in the Bundle, if specified in the config
        if self.config.config_dict["normalize_objects"]:
            bundle.normalize_objects()
        # Build the Object history for the Bundle
        object_history = ObjectHistory()
        object_history.build(bundle)
        # Create the actual Indicators derived from the Bundle
        self.create_bundle_indicators(object_history, ttp_id)

    def parse_malware_subject(self, malware_subject):
        """Parse a MAEC Malware Subject."""
        # Parse the Findings Bundles in the Malware Subject
        if malware_subject.findings_bundles and malware_subject.findings_bundles.bundle:
            # Create the STIX Package if it does not exist yet
            if not self.stix_package:
                self.stix_package = self.create_stix_package()
            # Create and add the STIX TTP for the Malware Subject
            ttp_id = self.add_stix_ttp(malware_subject)
            for bundle in malware_subject.findings_bundles.bundle:
                self.parse_bundle(bundle, ttp_id)

    def parse_package(self):
        """Parse a MAEC Package."""
        if self.maec_package.malware_subjects:
            for malware_subject in self.maec_package.malware_subjects:
                self.parse_malware_subject(malware_subject)