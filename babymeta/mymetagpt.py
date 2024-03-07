from abc import ABC
from typing import TypeVar, Type

from langchain.output_parsers import PydanticOutputParser
from metagpt.actions import Action
from metagpt.environment import Environment
from metagpt.logs import logger
from metagpt.roles import Role
from metagpt.schema import Message
from pydantic import BaseModel


class InstructAction(Action, ABC):
    class DirectOutput:
        T = TypeVar("T", bound=BaseModel)

        def __init__(self, content: T):
            self.content = content

    _Type_Input: TypeVar = TypeVar("_Type_Input", bound=BaseModel)
    _Type_Output: TypeVar = TypeVar("_Type_Output", bound=BaseModel)

    output_type: Type[_Type_Output]

    async def run(self, input_instruct: _Type_Input, env: Type[Environment]) -> DirectOutput | Message | str:
        raise NotImplementedError

    async def parse_output_prompt(self, answer: str) -> _Type_Output:
        """
        TODO: 容错
        :param answer:
        :return:
        """
        parser = PydanticOutputParser(pydantic_object=self.output_type)

        PROMPT_TEMPLATE = """
                The answer is: 
                {answer}
                -------------------------
                Thanks for your help. Now I need you to do me a favor to output the result in a specific format.
                Format is given below:
                {format}
                -------------------------
                Please enter your answer below:
                """

        prompt = PROMPT_TEMPLATE.format(answer=answer, format=parser.get_format_instructions())

        return parser.parse(await self._aask(prompt))


class InstructRole(Role, ABC):

    async def react(self) -> Message:
        logger.info(f"{self._setting}: Working")
        return await super().react()

    async def _act(self) -> Message:
        logger.info(f"{self._setting}: to do {self.rc.todo}({self.rc.todo.name})")
        msg = self.latest_observed_msg
        assert isinstance(self.todo, InstructAction), type(self.todo)

        response = await self.todo.run(up=msg.instruct_content, env=self.rc.env)

        match response:
            case InstructAction.DirectOutput():
                msg = Message(
                    content="",
                    instruct_content=response.content,
                    role=self._setting,
                    cause_by=self.todo,
                    sent_from=self,
                )
            case Message():
                msg = response
            case str():
                msg = Message(content=response, role=self.profile, cause_by=self.rc.todo, sent_from=self)
            case _:
                raise ValueError(f"Unknown response type: {type(response)}")

        self.rc.memory.add(msg)

        return msg


def match_action(action: str) -> str:
    """
    TODO: 诡异的函数
    :param action: example: "metagpt.actions.UserRequirement"
    :return:
    """
    return action.split(".")[-1]
