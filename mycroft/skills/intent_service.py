# Copyright 2017 Mycroft AI Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
import time

from adapt.context import ContextManagerFrame
from adapt.engine import IntentDeterminationEngine

from mycroft.configuration import Configuration
from mycroft.messagebus.message import Message
from mycroft.skills.core import open_intent_envelope
from mycroft.util.log import LOG
from mycroft.util.parse import normalize


class ContextManager(object):
    """
    ContextManager
    Use to track context throughout the course of a conversational session.
    How to manage a session's lifecycle is not captured here.
    """

    def __init__(self, timeout):
        self.frame_stack = []
        self.timeout = timeout * 60  # minutes to seconds

    def clear_context(self):
        self.frame_stack = []

    def remove_context(self, context_id):
        self.frame_stack = [(f, t) for (f, t) in self.frame_stack
                            if context_id in f.entities[0].get('data', [])]

    def inject_context(self, entity, metadata={}):
        """
        Args:
            entity(object):
                format {'data': 'Entity tag as <str>',
                        'key': 'entity proper name as <str>',
                         'confidence': <float>'
                         }
            metadata(object): dict, arbitrary metadata about the entity being
            added
        """
        try:
            if len(self.frame_stack) > 0:
                top_frame = self.frame_stack[0]
            else:
                top_frame = None
            if top_frame and top_frame[0].metadata_matches(metadata):
                top_frame[0].merge_context(entity, metadata)
            else:
                frame = ContextManagerFrame(entities=[entity],
                                            metadata=metadata.copy())
                self.frame_stack.insert(0, (frame, time.time()))
        except (IndexError, KeyError):
            pass

    def get_context(self, max_frames=None, missing_entities=None):
        """
        Constructs a list of entities from the context.

        Args:
            max_frames(int): maximum number of frames to look back
            missing_entities(list of str): a list or set of tag names,
            as strings

        Returns:
            list: a list of entities
        """
        missing_entities = missing_entities or []

        relevant_frames = [frame[0] for frame in self.frame_stack if
                           time.time() - frame[1] < self.timeout]
        if not max_frames or max_frames > len(relevant_frames):
            max_frames = len(relevant_frames)

        missing_entities = list(missing_entities)
        context = []
        for i in xrange(max_frames):
            frame_entities = [entity.copy() for entity in
                              relevant_frames[i].entities]
            for entity in frame_entities:
                entity['confidence'] = entity.get('confidence', 1.0) \
                    / (2.0 + i)
            context += frame_entities

        result = []
        if len(missing_entities) > 0:
            for entity in context:
                if entity.get('data') in missing_entities:
                    result.append(entity)
                    # NOTE: this implies that we will only ever get one
                    # of an entity kind from context, unless specified
                    # multiple times in missing_entities. Cannot get
                    # an arbitrary number of an entity kind.
                    missing_entities.remove(entity.get('data'))
        else:
            result = context

        # Only use the latest instance of each keyword
        stripped = []
        processed = []
        for f in result:
            keyword = f['data'][0][1]
            if keyword not in processed:
                stripped.append(f)
                processed.append(keyword)
        result = stripped
        return result


class IntentService(object):
    def __init__(self, emitter):
        self.config = Configuration.get().get('context', {})
        self.engine = IntentDeterminationEngine()
        self.context_keywords = self.config.get('keywords', [])
        self.context_max_frames = self.config.get('max_frames', 3)
        self.context_timeout = self.config.get('timeout', 2)
        self.context_greedy = self.config.get('greedy', False)
        self.context_manager = ContextManager(self.context_timeout)
        self.emitter = emitter
        self.emitter.on('register_vocab', self.handle_register_vocab)
        self.emitter.on('register_intent', self.handle_register_intent)
        self.emitter.on('recognizer_loop:utterance', self.handle_utterance)
        self.emitter.on('detach_intent', self.handle_detach_intent)
        self.emitter.on('detach_skill', self.handle_detach_skill)
        # Context related handlers
        self.emitter.on('add_context', self.handle_add_context)
        self.emitter.on('remove_context', self.handle_remove_context)
        self.emitter.on('clear_context', self.handle_clear_context)
        # Converse method
        self.emitter.on('skill.converse.response',
                        self.handle_converse_response)
        self.active_skills = []  # [skill_id , timestamp]
        self.converse_timeout = 5  # minutes to prune active_skills

    def do_converse(self, utterances, skill_id, lang):
        self.emitter.emit(Message("skill.converse.request", {
            "skill_id": skill_id, "utterances": utterances, "lang": lang}))
        self.waiting = True
        self.result = False
        start_time = time.time()
        t = 0
        while self.waiting and t < 5:
            t = time.time() - start_time
            time.sleep(0.1)
        self.waiting = False
        return self.result

    def handle_converse_response(self, message):
        # id = message.data["skill_id"]
        # no need to crosscheck id because waiting before new request is made
        # no other skill will make this request is safe assumption
        result = message.data["result"]
        self.result = result
        self.waiting = False

    def remove_active_skill(self, skill_id):
        for skill in self.active_skills:
            if skill[0] == skill_id:
                self.active_skills.remove(skill)

    def add_active_skill(self, skill_id):
        # search the list for an existing entry that already contains it
        # and remove that reference
        self.remove_active_skill(skill_id)
        # add skill with timestamp to start of skill_list
        self.active_skills.insert(0, [skill_id, time.time()])

    def update_context(self, intent):
        """
            updates context with keyword from the intent.

            NOTE: This method currently won't handle one_of intent keywords
                  since it's not using quite the same format as other intent
                  keywords. This is under investigation in adapt, PR pending.

            Args:
                intent: Intent to scan for keywords
        """
        for tag in intent['__tags__']:
            if 'entities' not in tag:
                continue
            context_entity = tag['entities'][0]
            if self.context_greedy:
                self.context_manager.inject_context(context_entity)
            elif context_entity['data'][0][1] in self.context_keywords:
                self.context_manager.inject_context(context_entity)

    def handle_utterance(self, message):
        # Get language of the utterance
        lang = message.data.get('lang', None)
        if not lang:
            lang = "en-us"

        utterances = message.data.get('utterances', '')

        # check for conversation time-out
        self.active_skills = [skill for skill in self.active_skills
                              if time.time() - skill[
                                  1] <= self.converse_timeout * 60]

        # check if any skill wants to handle utterance
        for skill in self.active_skills:
            if self.do_converse(utterances, skill[0], lang):
                # update timestamp, or there will be a timeout where
                # intent stops conversing whether its being used or not
                self.add_active_skill(skill[0])
                return

        # no skill wants to handle utterance
        best_intent = None
        for utterance in utterances:
            try:
                # normalize() changes "it's a boy" to "it is boy", etc.
                best_intent = next(self.engine.determine_intent(
                    normalize(utterance, lang), 100,
                    include_tags=True,
                    context_manager=self.context_manager))
                # TODO - Should Adapt handle this?
                best_intent['utterance'] = utterance
            except StopIteration, e:
                LOG.exception(e)
                continue

        if best_intent and best_intent.get('confidence', 0.0) > 0.0:
            self.update_context(best_intent)
            reply = message.reply(
                best_intent.get('intent_type'), best_intent)
            self.emitter.emit(reply)
            # update active skills
            skill_id = int(best_intent['intent_type'].split(":")[0])
            self.add_active_skill(skill_id)

        else:
            self.emitter.emit(Message("intent_failure", {
                "utterance": utterances[0],
                "lang": lang
            }))

    def handle_register_vocab(self, message):
        start_concept = message.data.get('start')
        end_concept = message.data.get('end')
        regex_str = message.data.get('regex')
        alias_of = message.data.get('alias_of')
        if regex_str:
            self.engine.register_regex_entity(regex_str)
        else:
            self.engine.register_entity(
                start_concept, end_concept, alias_of=alias_of)

    def handle_register_intent(self, message):
        print "Registering: " + str(message.data)
        intent = open_intent_envelope(message)
        self.engine.register_intent_parser(intent)

    def handle_detach_intent(self, message):
        intent_name = message.data.get('intent_name')
        new_parsers = [
            p for p in self.engine.intent_parsers if p.name != intent_name]
        self.engine.intent_parsers = new_parsers

    def handle_detach_skill(self, message):
        skill_id = message.data.get('skill_id')
        new_parsers = [
            p for p in self.engine.intent_parsers if
            not p.name.startswith(skill_id)]
        self.engine.intent_parsers = new_parsers

    def handle_add_context(self, message):
        """
            Handles adding context from the message bus.
            The data field must contain a context keyword and
            may contain a word if a specific word should be injected
            as a match for the provided context keyword.

        """
        entity = {'confidence': 1.0}
        context = message.data.get('context')
        word = message.data.get('word') or ''
        # if not a string type try creating a string from it
        if not isinstance(word, basestring):
            word = str(word)
        entity['data'] = [(word, context)]
        entity['match'] = word
        entity['key'] = word
        self.context_manager.inject_context(entity)

    def handle_remove_context(self, message):
        """
            Handles removing context from the message bus. The
            data field must contain the 'context' to remove.
        """
        context = message.data.get('context')
        if context:
            self.context_manager.remove_context(context)

    def handle_clear_context(self, message):
        """
            Clears all keywords from context.
        """
        self.context_manager.clear_context()
